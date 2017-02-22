'''
===============
=== Purpose ===
===============

Fetches github repos and "deploys" them on the delphi server. (Aka, push to
production.) Deployment consists of compiling, minimizing, copying, etc.

Alternatively, a repo (or any other data) contained in a local tar or zip file
can be deployed in the same way as an ordinary repo. This is useful for testing
uncommitted changes, deploying unhosted projects, and deploying files which
are not suitable for version control (e.g. binaries).

See also:
  - https://github.com/cmu-delphi/github-deploy-repo
  - https://developer.github.com/webhooks/
  - /home/automation/public_html/github-webhook.php


=====================
=== Configuration ===
=====================

This program follows deployment instructions defined in a JSON file. By
default, this file is named "deploy.json" and lives in the root of the
git repository. Most fields should be self-explanatory, but the various
commands ("actions") are described below.

  - [copy] Copies a file. Note that the source file must reside with the
    repository (e.g. it will refuse to copy /etc/passwd). The destination
    file, however, can be anywhere in the filesystem (well, anywhere the
    user has write access). Existing files will be overwritten. Additional
    fields:
    - [src] The source file. (required)
    - [dst] The destination file. (required)
    - [match] A regular expression. The action is applied to all files whose
      basename matches the regex. If this field is present, `src` and `dst` are
      interpreted as directories instead of files. (optional)
    - [add-header-comment] Whether to include a header comment (containing a
      warning not to edit the file and a pointer to the repository) at the top
      of the destination file. (optional)
    - [replace-keywords] Replace template strings in source file with values
      found in the list of template files. Each file is a JSON object
      containing a list of (key, value) pairs to be replaced. (optional)

  - [move] Identical to the `copy` command except the source file is deleted.

  - [compile-coffee] Transpiles a CoffeeScript file to a JavaScript file.
    Additional fields:
    - [src] The input file. (required)
    - [dst] The output file. Default is `src` with extension replaced with "js"
      unless otherwise specified. (optional)

  - [minimize-js] Minimize a JavaScript file. Additional fields:
    - [src] The input file. (required)
    - [dst] The output file. Defaults to `src` unless otherwise specified.
      (optional)

  - [export] Makes a file available to other repos (via `import`) by placing it
    in a shared directory. (This is essentially a `copy` action with a
    predefined `dst` path.) Additional fields:
    - [src] The file to export. (required)
    - [name] The name to use in the shared directory. Defaults to the basename
      of `src`. Can be used, for example, for versioning. (optional)
    - [add-header-comment] See `copy`. (optional)
    - [replace-keywords] See `copy`. (optional)

  - [import] Creates a symbolic link pointing to files placed (via `export`) in
     the shared directory. Additional fields:
     - [dst] The destination link. (required)
     - [name] The name to use in the shared directory. Defaults to the basename
       of `dst`. Can be used, for example, for versioning. (optional)


=======================
=== Data Dictionary ===
=======================

`github_deploy_repo` is the table where repo information is stored.
+----------+--------------+------+-----+---------------+----------------+
| Field    | Type         | Null | Key | Default       | Extra          |
+----------+--------------+------+-----+---------------+----------------+
| id       | int(11)      | NO   | PRI | NULL          | auto_increment |
| repo     | varchar(128) | NO   | UNI | NULL          |                |
| commit   | char(40)     | NO   |     | 0000[...]0000 |                |
| datetime | datetime     | NO   |     | NULL          |                |
| status   | int(11)      | NO   |     | 0             |                |
+----------+--------------+------+-----+---------------+----------------+
id: unique identifier for each record
repo: the name of the github repo (in the form of "owner/name")
commit: hash of the latest commit
datetime: the date and time of the last status update
status: one of 0 (queued), 1 (success), 2 (skipped), or -1 (failed)


=================
=== Changelog ===
=================

2017-02-22
  + support zipped github repos (with deploy.json at second level)
2017-02-21
  + deploy a tar/zip package (with deploy.json at first level)
2017-02-06
  + path substitution using the "paths" object
2016-12-15
  + include timestamp in header
2016-12-12
  * create destination for `import` files
2016-12-12
  * better error handling for `import`
2016-12-09
  + commit hash in header comment
  + `export` and `import` actions
  * refactoring of function `execute`
2016-11-10
  * compile-coffee creates *.js by default
  * minimize-js overwrites `src` by default
2016-11-09
  + support header for htaccess files
  + treat actions of type string as comments
  + `move` action
  + match files for copy/move with optional regex
2016-11-05
  * fancier header for generated files
2016-11-03
  * create directories when copying files
2016-10-28
  + support header for PHP files
  * fix newlines when replacing keywords
  * fix copy to non-web locations
  * use python secrets
2016-10-21
  + templating via "replace-keywords"
2016-10-20
  + switch database and store deploy status
2016-10-17
  * original version
'''

# standard library
import argparse
import datetime
import glob
import json
import os
import os.path
import re
import shutil
import subprocess
import sys
import time
# third party
import mysql.connector
# first party
import extractor
import secrets


# header for generated files
HEADER_WIDTH = 55
HEADER_LINES = [
  # from the command line, run: figlet "DO NOT EDIT"
  ' ____   ___    _   _  ___ _____   _____ ____ ___ _____ ',
  '|  _ \ / _ \  | \ | |/ _ \_   _| | ____|  _ \_ _|_   _|',
  '| | | | | | | |  \| | | | || |   |  _| | | | | |  | |  ',
  '| |_| | |_| | | |\  | |_| || |   | |___| |_| | |  | |  ',
  '|____/ \___/  |_| \_|\___/ |_|   |_____|____/___| |_|  ',
]


def get_substituted_path(path, substitutions):
  for key, value in substitutions.items():
    pattern = '[[%s]]' % key
    if pattern in path:
      path = path.replace(pattern, value)
  return path


def get_file(name, path=None, substitutions={}):
  new_name = get_substituted_path(name, substitutions)
  if new_name != name:
    print('substituted [%s] -> [%s]' % (name, new_name))
    name = new_name
  if path is not None:
    name = os.path.join(path, name)
  absname = os.path.abspath(name)
  path, name = os.path.split(absname)
  if '.' in name:
    ext = name[name.index('.') + 1:]
  else:
    ext = ''
  return absname, path, name, ext


def check_file(abspath, path):
  source_dir = get_file(path)[0]
  if not abspath.startswith(source_dir):
    raise Exception('file [%s] is not inside [%s]' % (abspath, source_dir))


def add_header(repo_link, commit, src, dst_ext):
  # build the header based on the source language
  ext = dst_ext.lower()
  pre_block, post_block, pre_line, post_line = '', '', '', ''
  blanks = '\n\n\n'
  if ext in ('html', 'xml'):
    pre_block, post_block = '<!--\n', '-->\n' + blanks
  elif ext in ('js', 'min.js', 'css', 'c', 'cpp', 'h', 'hpp', 'java'):
    pre_block, post_block = '/*\n', '*/\n' + blanks
  elif ext in ('py', 'r', 'coffee', 'htaccess'):
    pre_line, post_line, post_block = '# ', ' #', blanks
  elif ext in ('php'):
    # be sure to not introduce whitespace (e.g. newlines) outside php tags
    pre_block, post_block = '<?php /*\n', '*/\n' + blanks + '?>'
  else:
    # nothing modified, return the original file
    print(' warning: skipped header for file extension [%s]' % dst_ext)
    return src

  # additional header lines
  t = round(time.time())
  dt = datetime.datetime.fromtimestamp(t).isoformat(' ')
  lines = [
    '',
    'Automatically generated from sources at:',
    repo_link,
    '',
    ('Commit hash: %s' % commit),
    ('Deployed at: %s (%d)' % (dt, t)),
  ]

  # add the header to a copy of the source file
  tmp = get_file(src[0] + '__header')
  print(' adding header [%s] -> [%s]' % (src[0], tmp[0]))
  with open(tmp[0], 'wb') as fout:
    fout.write(bytes(pre_block, 'utf-8'))
    for line in HEADER_LINES + [line.center(HEADER_WIDTH) for line in lines]:
      fout.write(bytes(pre_line + line + post_line + '\n', 'utf-8'))
    fout.write(bytes(post_block, 'utf-8'))
    with open(src[0], 'rb') as fin:
      fout.write(fin.read())

  # return the new file
  return tmp


def replace_keywords(src, templates):
  # load list of (key, value) pairs
  pairs = []
  for t in templates:
    with open(t[0], 'r') as f:
      pairs.extend(json.loads(f.read()))

  # make a new file to hold the results
  tmp = get_file(src[0] + '__valued')
  print(' replacing %d keywords [%s] -> [%s]' % (len(pairs), src[0], tmp[0]))
  with open(tmp[0], 'w') as fout:
    with open(src[0], 'r') as fin:
      for line in fin.readlines():
        for (k, v) in pairs:
          line = line.replace(k, v)
        fout.write(line)

  # return the new file
  return tmp


def copymove_single(repo_link, commit, path, row, src, dst, is_move):
  action = 'move' if is_move else 'copy'
  print(' %s %s -> %s' % (action, src[2], dst[2]))
  # check access
  check_file(src[0], path)
  # put a big "do not edit" warning at the top of the file
  if row.get('add-header-comment', False) is True:
    src = add_header(repo_link, commit, src, dst[3])
  # replace template keywords with values
  templates = row.get('replace-keywords')
  if type(templates) is str:
    templates = [templates]
  if type(templates) in (tuple, list):
    src = replace_keywords(src, [get_file(t, path) for t in templates])
  # make the copy (method depends on destination)
  if dst[0].startswith('/var/www/html/'):
    # copy to staging area
    tmp = get_file(src[2] + '__tmp', '/common/')
    print(' [%s] -> [%s]' % (src[0], tmp[0]))
    shutil.copy(src[0], tmp[0])
    # make directory and move the file as user `webadmin`
    cmd = "sudo -u webadmin -s mkdir -p '%s'" % (dst[1])
    print('  [%s]' % cmd)
    subprocess.check_call(cmd, shell=True)
    cmd = "sudo -u webadmin -s mv -fv '%s' '%s'" % (tmp[0], dst[0])
    print('  [%s]' % cmd)
    subprocess.check_call(cmd, shell=True)
  else:
    # make directory and copy the file
    print(' [%s] -> [%s]' % (src[0], dst[0]))
    os.makedirs(dst[1], exist_ok=True)
    shutil.copy(src[0], dst[0])
  # maybe delete the source file
  if is_move:
    os.remove(src[0])


def copymove(repo_link, commit, path, row, substitutions):
  # {copy|move} <src> <dst> [add-header-comment] [replace-keywords]
  src = get_file(row['src'], path, substitutions)
  dst = get_file(row['dst'], path, substitutions)
  # determine which file(s) should be used
  if 'match' in row:
    sources, destinations = [], []
    for name in glob.glob(os.path.join(src[0], '*')):
      src2 = get_file(name)
      basename = src2[2]
      if re.match(row['match'], basename) is not None:
        sources.append(src2)
        destinations.append(get_file(os.path.join(dst[0], basename)))
  else:
    sources, destinations = [src], [dst]
  # apply the action to each file
  is_move = row.get('type').lower() == 'move'
  for src, dst in zip(sources, destinations):
    copymove_single(repo_link, commit, path, row, src, dst, is_move)


def compile_coffee(repo_link, commit, path, row, substitutions):
  # compile-coffee <src> [dst]
  src = get_file(row['src'], path, substitutions)
  if 'dst' in row:
    dst = get_file(row['dst'], path, substitutions)
  else:
    basename, extension = src[2:4]
    if extension != '':
      basename = basename[:-len(extension)] + 'js'
    else:
      basename += '.js'
    dst = get_file(basename, src[1])
  # check access
  check_file(src[0], path)
  # compile
  action = row.get('type').lower()
  print(' %s %s -> %s' % (action, src[2], dst[2]))
  cmd = "coffee -c -p '%s' > '%s'" % (src[0], dst[0])
  print('  [%s]' % cmd)
  subprocess.check_call(cmd, shell=True)


def minimize_js(repo_link, commit, path, row, substitutions):
  # minimize-js <src> [dst]
  src = get_file(row['src'], path, substitutions)
  if 'dst' in row:
    dst = get_file(row['dst'], path, substitutions)
  else:
    dst = src
  # check access
  check_file(src[0], path)
  # minimize
  action = row.get('type').lower()
  print(' %s %s -> %s' % (action, src[2], dst[2]))
  cmd = "uglifyjs '%s' -c -m -o '%s'" % (src[0], dst[0])
  print('  [%s]' % cmd)
  subprocess.check_call(cmd, shell=True)


def action_export(repo_link, commit, path, row, substitutions):
  # export <src> [name]
  src = get_file(row['src'], path, substitutions)
  basename = get_file(row.get('name', src[2]))[2]
  dst = get_file(basename, 'exports/', substitutions)
  # copy to shared directory
  print(' export %s -> %s' % (src[0], dst[0]))
  copymove_single(repo_link, commit, path, row, src, dst, False)


def action_import(repo_link, commit, path, row, substitutions):
  # import <name> <dst>
  dst = get_file(row['dst'], path, substitutions)
  basename = get_file(row.get('name', dst[2]))[2]
  src = get_file(basename, 'exports/', substitutions)
  # link to shared directory
  print(' import %s <- %s' % (src[0], dst[0]))
  os.makedirs(dst[1], exist_ok=True)
  if not os.path.exists(dst[0]):
    os.symlink(src[0], dst[0])
    print(' created symlink')
  elif os.path.islink(dst[0]):
    print(' symlink with destination name already exists')
  else:
    raise Exception('object with destination name already exists')


def execute(repo_link, commit, path, config):
  # magic and versioning
  typestr = 'delphi deploy config'
  v_min = v_max = 1

  # parse the config file
  with open(os.path.join(path, config)) as f:
    cfg = json.loads(f.read())

  # cfg better be a map/dictionary
  if type(cfg) is not dict:
    raise Exception('unable to load deploy config file')

  # sanity checks
  for (name, result) in [
    ['type', cfg.get('type') == typestr],
    ['version', v_min <= cfg.get('version', 0) <= v_max],
    ['actions', type(cfg.get('actions')) is list],
  ]:
    if not result:
      raise Exception('missing or invalid deploy config `%s`' % name)

  # just in case
  if cfg.get('skip', False) is True:
    print('field `skip` is present and true - skipping deploy')
    return

  # optional path substitution
  paths = cfg.get('paths', {})
  if len(paths) > 0:
    print('will substitute the following path fragments:')
    for key, value in paths.items():
      print(' [[%s]] -> %s' % (key, value))

  # execute actions sequentially
  actions = cfg['actions']
  executors = {
    'copy': copymove,
    'move': copymove,
    'compile-coffee': compile_coffee,
    'minimize-js': minimize_js,
    'export': action_export,
    'import': action_import,
  }
  for (idx, row) in enumerate(actions):
    # each row should be either: a map/dict/object with a string field named
    #   "type", or a comment string
    if type(row) == str:
      continue
    elif type(row) != dict or 'type' not in row or type(row['type']) != str:
      raise Exception('invalid action (%d/%d)' % (idx + 1, len(actions)))

    # handle the action based on its type
    action = row.get('type').lower()
    if action in executors:
      executors[action](repo_link, commit, path, row, paths)
    else:
      raise Exception('unsupported action: %s' % action)


def deploy_repo(cnx, owner, name):
  # try to deploy, but catch any exceptions that may arise
  exception = None
  status = -1
  commit = None
  try:
    # a place for temporary files
    tmpdir = 'github_deploy_repo__tmp'
    os.makedirs(tmpdir)

    if owner == '<local>':
      # hash the file for record keeping
      sha1sum = subprocess.check_output("sha1sum '%s'" % name, shell=True)
      commit = sha1sum.decode('utf-8')[:40]
      url = 'file://%s' % name
      print('deploying package %s/%s (%s)' % (owner, name, url))
      print(' file SHA1 hash is %s' % commit)

      # extract the file
      extractor.Extractor.extract(name, tmpdir)

      # workaround for zipped github repos where deploy.json isn't at the root
      contents = glob.glob(os.path.join(tmpdir, '*'))
      if len(contents) == 1 and os.path.isdir(contents[0]):
        tmpdir2 = tmpdir + '2'
        # rename ./tmpdir/repo -> ./tmpdir2
        shutil.move(contents[0], tmpdir2)
        # delete ./tmpdir
        shutil.rmtree(tmpdir)
        # rename tmpdir2 -> ./tmpdir
        shutil.move(tmpdir2, tmpdir)
    else:
      # build the github repo link
      url = 'https://github.com/%s/%s.git' % (owner, name)
      print('deploying repo %s/%s (%s)' % (owner, name, url))

      # clone the repo
      cmd = 'git clone %s %s' % (url, tmpdir)
      subprocess.check_call(cmd, shell=True, timeout=60)

      # get the latest commit hash
      cmd = 'git --git-dir %s/.git rev-parse HEAD' % tmpdir
      commit = subprocess.check_output(cmd, shell=True)
      commit = str(commit, 'utf-8').strip()
      print(' most recent commit is %s' % commit)

    # deploy the repo
    config_name = 'deploy.json'
    config_file = os.path.join(tmpdir, config_name)
    if os.path.isfile(config_file):
      execute(url[:-4], commit, tmpdir, config_name)
      status = 1
    else:
      print('deploy config does not exist for this repo (%s)' % config_file)
      status = 2
  except Exception as ex:
    exception = ex

  # safely cleanup temporary files
  try:
    shutil.rmtree(tmpdir)
  except Exception as ex:
    if exception is None:
      exception = ex

  # update repo status
  set_repo_status(cnx, owner, name, commit, status)

  # throw the exception, if it exists
  if exception is not None:
    raise exception


def deploy_all(cnx, repos):
  # deploy one at a time, keeping track of any errors along the way
  exceptions = []
  for (owner, name) in repos:
    try:
      deploy_repo(cnx, owner, name)
    except Exception as ex:
      print('failed to deploy %s/%s - %s' % (str(owner), str(name), str(ex)))
      exceptions.append(ex)

  # throw the first exception, if there is one
  if len(exceptions) > 0:
    raise exceptions[0]


def get_repo_list(cnx):
  # pick all repos with status of 0
  cur = cnx.cursor()
  cur.execute("SELECT `repo` FROM `github_deploy_repo` WHERE `status` = 0")
  repos = [repo.split('/') for (repo,) in cur]
  cur.close()
  return repos


def set_repo_status(cnx, owner, name, commit, status):
  # update the repo status table
  repo = '%s/%s' % (owner, name)
  cur = cnx.cursor()

  # execute the proper update
  if commit is not None:
    args = (repo, commit, status, commit, status)
    cur.execute("""
      INSERT INTO `github_deploy_repo`
        (`repo`, `commit`, `datetime`, `status`)
      VALUES
        (%s, %s, now(), %s)
      ON DUPLICATE KEY UPDATE
        `commit` = %s, `datetime` = now(), status = %s
    """, args)
  else:
    args = (repo, status, status)
    cur.execute("""
      INSERT INTO `github_deploy_repo`
        (`repo`, `datetime`, `status`)
      VALUES
        (%s, now(), %s)
      ON DUPLICATE KEY UPDATE
        `datetime` = now(), status = %s
    """, args)

  # cleanup
  cur.close()
  cnx.commit()


def main():
  """Command line usage."""

  # args and usage
  parser = argparse.ArgumentParser()
  parser.add_argument(
    '-d', '--database',
    default=False,
    action='store_true',
    help='fetch list of repos from the database')
  parser.add_argument(
    '-r', '--repo',
    type=str,
    default=None,
    action='store',
    help='manually deploy the specified repo (e.g. cmu-delphi/www-nowcast)')
  parser.add_argument(
    '-p', '--package',
    type=str,
    default=None,
    action='store',
    help='manually deploy the specified tar/zip file (e.g. experimental.tgz)')
  args = parser.parse_args()

  # require exactly one deploy source
  count = lambda cond: 1 if cond else 0
  sources = count(args.database) + count(args.repo) + count(args.package)
  if sources != 1:
    print('Exactly one deploy source must be given.')
    parser.print_help()
    sys.exit(0)

  # database setup
  u, p = secrets.db.auto
  cnx = mysql.connector.connect(user=u, password=p, database='utils')

  if args.database:
    # deploy github repos from the database
    repos = get_repo_list(cnx)
    if len(repos) > 0:
      print('will deploy the following repos:')
      for (owner, name) in repos:
        print(' %s/%s' % (owner, name))
      deploy_all(cnx, repos)
    else:
      print('no repos to deploy')
  elif args.repo:
    # deploy a specific github repo
    owner, name = args.repo.split('/')
    deploy_repo(cnx, owner, name)
  elif args.package:
    # deploy a local tar/zip file as if it were a repo
    owner, name = '<local>', args.package
    deploy_repo(cnx, owner, name)

  # database cleanup
  cnx.close()


if __name__ == '__main__':
  main()
