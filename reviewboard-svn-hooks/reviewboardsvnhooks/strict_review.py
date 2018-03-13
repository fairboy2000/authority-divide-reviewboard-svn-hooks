#!/usr/bin/python
# -*- coding:utf8 -*-
import os
import sys
import subprocess
import urllib2
import cookielib
import base64
import re
import shelve
import datetime
import ConfigParser
import shutil

try:
    import json
except ImportError:
    import simplejson as json

from urlparse import urljoin

from .utils import get_cmd_output, split

def get_os_conf_dir():
    platform = sys.platform
    if platform.startswith('win'):
        try:
            return os.environ['ALLUSERSPROFILE']
        except KeyError:
            print >>sys.stderr, 'Unspported operation system:%s'%platform
            sys.exit(1)
    return '/etc'

def get_os_temp_dir():
    import tempfile
    return tempfile.gettempdir()

def get_os_log_dir():
    platform = sys.platform
    if platform.startswith('win'):
        return get_os_conf_dir()
    return '/var/log'

OS_CONF_DIR = get_os_conf_dir()

conf = ConfigParser.ConfigParser()

conf_file = os.path.join(OS_CONF_DIR, 'reviewboard-svn-hooks', 'conf.ini')
if not conf.read(conf_file):
    raise StandardError('invalid configuration file:%s'%conf_file)


COOKIE_FILE = os.path.join(get_os_temp_dir(), 'reviewboard-svn-hooks-cookies.txt')

DEBUG = conf.getint('common', 'debug')

def debug(s):
    if not DEBUG:
        return
    f = open(os.path.join(get_os_log_dir(), 'reviewboard-svn-hooks', 'debug.log'), 'at')
    print >>f, str(datetime.datetime.now()), s
    f.close()

def create_repos_relevant_experts_list(file, REVIEW_PATH):
    adjust_review_path_list = []
    for single_path in REVIEW_PATH:
        adjust_review_path_list.append(single_path.strip())
    repos_relevant_experts_list = []
    repos_urgent_experts_list = []
    config = ConfigParser.ConfigParser()
    config.readfp(open(file), 'rb')
    for single_arp in adjust_review_path_list:
        try:
            experts = config.get('rule', single_arp)
        except:
            continue
        if experts:
            for expert in experts.split(','):
                repos_relevant_experts_list.append(single_arp + '{{' + expert)
    urgent_experts = config.get('rule', 'urgent_experts')
    for ue_expert in urgent_experts.split(','):
        repos_urgent_experts_list.append(ue_expert)
    return repos_relevant_experts_list, repos_urgent_experts_list

RB_SERVER = conf.get('reviewboard', 'url')
USERNAME = conf.get('reviewboard', 'username')
PASSWORD = conf.get('reviewboard', 'password')

MIN_SHIP_IT_COUNT = conf.getint('rule', 'min_ship_it_count')
MIN_EXPERT_SHIP_IT_COUNT = conf.getint('rule', 'min_expert_ship_it_count')
review_path = conf.get('rule', 'review_path')
REVIEW_PATH = split(review_path)
ignore_path = conf.get('rule', 'ignore_path')
IGNORE_PATH = split(ignore_path)
repos_relevant_experts_list, repos_urgent_experts_list = create_repos_relevant_experts_list(conf_file, REVIEW_PATH)

class SvnError(StandardError):
    pass

class Opener(object):
    def __init__(self, server, username, password, cookie_file = None):
        self._server = server
        if cookie_file is None:
            cookie_file = COOKIE_FILE
        self._auth = base64.b64encode(username + ':' + password)
        cookie_jar = cookielib.MozillaCookieJar(cookie_file)
        cookie_handler = urllib2.HTTPCookieProcessor(cookie_jar)
        self._opener = urllib2.build_opener(cookie_handler)

    def open(self, path, ext_headers, *a, **k):
        url = urljoin(self._server, path)
        return self.abs_open(url, ext_headers, *a, **k)

    def abs_open(self, url, ext_headers, *a, **k):
        debug('url open:%s' % url)
        r = urllib2.Request(url)
        for k, v in ext_headers:
            r.add_header(k, v)
        r.add_header('Authorization', 'Basic ' + self._auth)
        try:
            rsp = self._opener.open(r)
            return rsp.read()
        except urllib2.URLError, e:
            raise SvnError(str(e))

def make_svnlook_cmd(directive, repos, txn):
    def get_svnlook():
        platform = sys.platform
        if platform.startswith('win'):
            return get_cmd_output(['where svnlook']).split('\n')[0].strip()
        return 'svnlook'

    cmd =[get_svnlook(), directive, '-t',  txn, repos]
    debug(cmd)
    return cmd

def get_review_id(repos, txn):
    svnlook = make_svnlook_cmd('log', repos, txn)
    log = get_cmd_output(svnlook)
    debug(log)
    rid = re.search(r'review:\d+', log, re.M | re.I)
    if rid:
        return rid.group().split(':')[1]
    raise SvnError('No review id.')

def add_to_rid_db(rid, file_path):
    USED_RID_DB = shelve.open(file_path + '/rb-svn-hooks-used-rid.db')
    if USED_RID_DB.has_key(rid):
        raise SvnError, "review-id(%s) is already used."%rid
    USED_RID_DB[rid] = rid
    USED_RID_DB.sync()
    USED_RID_DB.close()

def get_relevant_experts(repos, repos_relevant_experts_list):
    relevant_experts = []
    if repos_relevant_experts_list:
        for single_comb in repos_relevant_experts_list:
            if repos.strip() == single_comb.split('{{')[0].strip():
                relevant_experts.append(single_comb.split('{{')[1].strip())
    return relevant_experts

def check_rb(repos, txn, repos_relevant_experts_list, file_path):
    relevant_experts = get_relevant_experts(repos, repos_relevant_experts_list)
    rid = get_review_id(repos, txn)
    path = 'api/review-requests/' + str(rid) + '/reviews/'
    opener = Opener(RB_SERVER, USERNAME, PASSWORD)
    rsp = opener.open(path, {})
    reviews = json.loads(rsp)
    if reviews['stat'] != 'ok':
        raise SvnError, "get reviews error."
    ship_it_users = set()
    for item in reviews['reviews']:
        ship_it = int(item['ship_it'])
        if ship_it:
            ship_it_users.add(item['links']['user']['title'])
    
    if len(ship_it_users) < MIN_SHIP_IT_COUNT:
        raise SvnError, "not enough of ship_it."
    expert_count = 0
    if relevant_experts:
        for user in ship_it_users:
            for single_expert in relevant_experts:
                if user.strip() == single_expert.strip():
                    expert_count += 1
            for urgent_expert in repos_urgent_experts_list:
                if user.strip() == urgent_expert.strip():
                    expert_count += 1
    if expert_count < MIN_EXPERT_SHIP_IT_COUNT:
        raise SvnError, 'not enough of key user ship_it.'
    add_to_rid_db(rid, file_path)
    return relevant_experts, rid, path, opener, reviews, ship_it_users, expert_count

def is_ignorable(repos):
    if repos in IGNORE_PATH:
        return True
    else:
        return False

def get_new_folder_number(file_path):
    if os.path.exists(file_path + '/PRE_COMMIT_folder_number.ini'):
        folder_config = ConfigParser.ConfigParser()
        folder_config.readfp(open(file_path + '/PRE_COMMIT_folder_number.ini'), 'rb')
        folder_number = folder_config.get('number', 'value')
        new_number = int(folder_number) + 1
        config = ConfigParser.ConfigParser()
        config.add_section('number')
        config.set('number', 'value', new_number)
        with open(file_path + '/PRE_COMMIT_folder_number.ini', 'w+') as fc:
            config.write(fc)
        return new_number
    else:
        cfg = ConfigParser.ConfigParser()
        cfg.add_section('number')
        cfg.set('number', 'value', 1)
        with open(file_path + '/PRE_COMMIT_folder_number.ini', 'w+') as f:
            cfg.write(f)
        return 1

def _main(file_path, work_path):
    if os.path.exists(file_path):
        shutil.rmtree(file_path)
    os.mkdir(file_path)
    log_txt = open(file_path + '/check.log', 'w')
    log_txt.write('file_path: ' + file_path + ';')
    debug('command:' + str(sys.argv))
    repos = sys.argv[1]
    txn = sys.argv[2]
    log_txt.write('\nrepos: ' + repos + ';\n')
    log_txt.write('txn: ' + txn + ';\n')
    svnlook = make_svnlook_cmd('changed', repos, txn)
    log_txt.write('svnlook: ')
    for sl in svnlook:
        log_txt.write(sl + ';')
    changed = get_cmd_output(svnlook)
    log_txt.write('\nchanged: ' + changed + ';\n')
    debug(changed)
    log_txt.write('REVIEW_PATH: ')
    for RP in REVIEW_PATH:
        log_txt.write(RP + ';')
    log_txt.write('\nignore_path: ' + ignore_path + ';\n')
    log_txt.write('IGNORE_PATH: ')
    for IP in IGNORE_PATH:
        log_txt.write(IP + ';')

    log_txt.write('\nrepos_relevant_experts_list: ')
    for rrel in repos_relevant_experts_list:
        log_txt.write(rrel + ';')
    log_txt.write('urgent_experts: ')
    for ue in repos_urgent_experts_list:
        log_txt.write(ue + ';')

    if is_ignorable(repos):
        log_txt.write('\nis_ignorable')
        log_txt.close()
        return

    relevant_experts, rid, path, opener, reviews, ship_it_users, expert_count = check_rb(repos, txn, repos_relevant_experts_list, work_path)
    log_txt.write('\nrelevant_experts: ')
    for re in relevant_experts:
        log_txt.write(re + ';')
    log_txt.write('\nrid: ' + str(rid) + ';\n')
    log_txt.write('path: ' + path + ';\n')
    log_txt.write('ship_it_users: ')
    for siu in ship_it_users:
        log_txt.write(siu + ';')
    log_txt.write('\nexpert_count: ' + str(expert_count) + ';')
    log_txt.close()
    return

def main():
    work_path = '/home/administrator/svn_check_review/PRE_COMMIT'
    folder_number = get_new_folder_number(work_path)
    file_path = work_path + '/pre_commit' + str(folder_number)
    try:
        _main(file_path, work_path)
    except SvnError, e:
        print >> sys.stderr, str(e)
        exit(1)
    except Exception, e:
        print >> sys.stderr, str(e)
        import traceback
        traceback.print_exc(file=sys.stderr)
        exit(1)
    else:
        exit(0)