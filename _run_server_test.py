# -*- coding: utf-8 -*-

import sys
import json
import os.path
import urllib
import urllib2
import urlparse
import argparse
import cookielib
from pprint import pprint

from datetime import datetime

import argparse

CUR_PATH = os.path.dirname(os.path.abspath(__file__))
PROJ_PATH = os.path.dirname(CUR_PATH)
sys.path.append(PROJ_PATH)

from montage import utils
from montage.log import script_log
from montage.utils import encode_dict_to_bytes

cookies = cookielib.LWPCookieJar()

handlers = [
    urllib2.HTTPHandler(),
    urllib2.HTTPCookieProcessor(cookies)
]

opener = urllib2.build_opener(*handlers)


def fetch(url, data=None):
    if not data:
        req = urllib2.Request(url)
    else:
        data_bytes = json.dumps(data)
        req = urllib2.Request(url, data_bytes,
                              {'Content-Type': 'application/json'})
    return opener.open(req)


@script_log.wrap('info', inject_as='act')
def fetch_json(url, data=None, act=None, **kw):
    su_to = kw.get('su_to')
    if su_to:
        url_su_to = urllib.quote_plus(su_to)
        if '?' in url:
            url += '&su_to=' + url_su_to
        else:
            url += '?su_to=' + url_su_to
    act['url'] = url
    res = fetch(url, data=data)
    data_dict = json.load(res)
    if kw.get('assert_success', True):
        try:
            assert data_dict['status'] == 'success'
        except AssertionError:
            import pdb;pdb.set_trace()
    return data_dict


def full_run(url_base, remote):
    # load home
    # resp = fetch(url_base).read()

    print '.. loaded the home page'

    # login as a maintainer
    # resp = fetch(url_base + '/complete_login').read()
    # resp_dict = json.loads(resp)
    # assert resp_dict['cookie']['username'] == 'Slaporte'

    # print '.. logged in'

    # add an organizer
    data = {'username': 'Slaporte'}
    resp_dict = fetch_json(url_base + '/admin/add_organizer', data)

    print '.. added %s as organizer' % data['username']

    # create a campaign
    data = {'name': 'Another Test Campaign 2016',
            'coordinators': [u'LilyOfTheWest',
                             u'Slaporte',
                             u'Jimbo Wales']}

    resp_dict = fetch_json(url_base + '/admin/new/campaign', data)

    campaign_id = resp_dict['data']['id']

    print '.. created campaign #%s' % campaign_id

    # edit campaign
    data = {'name': 'A demo campaign 2016'}
    resp_dict = fetch_json(url_base + '/admin/campaign/%s/edit' % campaign_id, data)

    print '.. edited campaign #%s' % campaign_id

    # add a coordinator to this new camapign
    data = {'username': 'Effeietsanders'}
    resp_dict = fetch_json(url_base + '/admin/add_coordinator/campaign/%s' % campaign_id, data)

    # add a coordinator to this new camapign
    data = {'username': u'Jean-Frédéric'}
    resp_dict = fetch_json(url_base + '/admin/add_coordinator/campaign/%s' % campaign_id, data)

    print '.. added %s as coordinator for campaign #%s' % (data['username'], campaign_id)

    # get the coordinator's index
    resp_dict = fetch_json(url_base + '/admin')

    print '.. loaded the coordinators index'

    campaign_id = resp_dict['data'][-1]['id']

    # get coordinator's view of the first campaign
    resp_dict = fetch_json(url_base + '/admin/campaign/%s' % campaign_id)

    print '.. loaded the coordinator view of campaign #%s' % campaign_id

    # for date inputs (like deadline_date below), the default format
    # is %Y-%m-%d %H:%M:%S

    # add a round to that campaign
    data = {'name': 'Test yes/no round',
            'vote_method': 'yesno',
            'quorum': 4,
            'deadline_date': datetime(2016, 10, 15).isoformat(),
            'jurors': [u'Slaporte',
                       u'MahmoudHashemi',
                       u'Effeietsanders',
                       u'Jean-Frédéric',
                       u'LilyOfTheWest']}

    resp_dict = fetch_json(url_base + '/admin/campaign/%s/new/round' % campaign_id, data)

    round_id = resp_dict['data']['id']

    print '.. added round #%s to campaign #%s' % (round_id, campaign_id)

    # add a round to that campaign
    data = {'name': 'Test rating round',
            'vote_method': 'rating',
            'quorum': 4,
            'deadline_date': datetime(2016, 10, 15).isoformat(),
            'jurors': [u'Slaporte',
                       u'MahmoudHashemi',
                       u'Effeietsanders',
                       u'Jean-Frédéric',
                       u'LilyOfTheWest']}

    resp_dict = fetch_json(url_base + '/admin/campaign/%s/new/round' % campaign_id, data)

    round_id2 = resp_dict['data']['id']

    print '.. added round #%s to campaign #%s' % (round_id2, campaign_id)

    # add a round to that campaign
    data = {'name': 'Test ranking round',
            'vote_method': 'ranking',
            'quorum': 4,
            'deadline_date': datetime(2016, 10, 15).isoformat(),
            'jurors': [u'Slaporte',
                       u'MahmoudHashemi',
                       u'Effeietsanders',
                       u'Jean-Frédéric',
                       u'LilyOfTheWest']}


    resp_dict = fetch_json(url_base + '/admin/campaign/%s/new/round' % campaign_id, data)

    round_id3 = resp_dict['data']['id']

    print '.. added round #%s to campaign #%s' % (round_id3, campaign_id)
    '''
    # edit the round description
    data = {'directions': 'these are new directions'}
    resp_dict = fetch_json(url_base + '/admin/round/%s/edit' % round_id, data)

    print '.. edited the directions of round #%s' % round_id
    '''

    gist_url = 'https://gist.githubusercontent.com/slaporte/7433943491098d770a8e9c41252e5424/raw/ca394147a841ea5f238502ffd07cbba54b9b1a6a/wlm2015_fr_500.csv'

    # import the initial set of images to the round
    if remote:
        data = {'import_method': 'category',
                'category': 'Images_from_Wiki_Loves_Monuments_2015_in_Pakistan'}
    else:
        data = {'import_method': 'gistcsv',
                'gist_url': gist_url}
    resp_dict = fetch_json(url_base + '/admin/round/%s/import' % round_id, data)

    print '.. loaded %s entries into round #%s' % ('_', round_id)

    # activate the round
    resp_dict = fetch_json(url_base + '/admin/round/%s/activate' % round_id, {'post': True})

    if remote:
        data = {'import_method': 'category',
                'category': 'Images_from_Wiki_Loves_Monuments_2015_in_Pakistan'}
    else:
        data = {'import_method': 'gistcsv',
                'gist_url': gist_url}
    resp_dict = fetch_json(url_base + '/admin/round/%s/import' % round_id2, data)

    print '.. loaded %s entries into round #%s' % ('_', round_id2)

    # active the round
    resp_dict = fetch_json(url_base + '/admin/round/%s/activate' % round_id2, {'post': True})


    if remote:
        data = {'import_method': 'category',
                'category': 'Images_from_Wiki_Loves_Monuments_2015_in_Pakistan'}
    else:
        data = {'import_method': 'gistcsv',
                'gist_url': gist_url}
    resp_dict = fetch_json(url_base + '/admin/round/%s/import' % round_id3, data)

    print '.. loaded %s entries into round #%s' % ('_', round_id3)

    # active the round
    resp_dict = fetch_json(url_base + '/admin/round/%s/activate' % round_id3, {'post': True})
    # TODO: check results?

    print '.. activated round #%s' % round_id

    # get the juror's index
    resp_dict = fetch_json(url_base + '/juror')

    round_id = resp_dict['data'][-1]['id']

    print '.. loaded the jurors index'

    # get the juror's view of the last round
    resp_dict = fetch_json(url_base + '/juror/round/%s' % round_id)

    print '.. loaded juror view of round #%s' % round_id

    # get the juror's next task
    # (optional count and offset params)
    resp_dict = fetch_json(url_base + '/juror/round/%s/tasks' % round_id)

    entry_id = resp_dict['data'][0]['round_entry_id']
    task_id = resp_dict['data'][0]['id']

    print '.. loaded task(s) from round #%s' % round_id

    # submit the juror's rating
    data = {'entry_id': entry_id,
            'task_id': task_id,
            'rating': '0.8'}

    resp_dict = fetch_json(url_base + '/juror/submit/rating', data)

    print '.. submitted rating on task #%s' % task_id

    # pause round
    resp_dict = fetch_json(url_base + '/admin/round/%s/pause' % round_id, {'post': True})

    print '.. paused round %s' % round_id

    # edit jurors
    data = {'new_jurors': [u'Slaporte',
                           u'MahmoudHashemi',
                           u'Effeietsanders',
                           u'Jean-Frédéric',
                           u'Jimbo Wales']}

    resp_dict = fetch_json(url_base + '/admin/round/%s/edit_jurors' % round_id, data)

    print '.. edited jurors for round #%s' % round_id

    # reassign tasks

    resp_dict = fetch_json(url_base + '/admin/round/%s/activate' % round_id, {'post': True})

    print '.. reactivated round %s' % round_id

    resp_dict = fetch_json(url_base + '/admin/round/%s/preview_results' % round_id3)

    print '.. previewed results for round #%s' % round_id3

    '''
    resp_dict = fetch_json(url_base + '/juror/round/%s/tasks' % round_id)

    print '.. loaded more tasks'

    data = {'ratings': []}
    for task in resp_dict['data']:
        bulk_rating = {'entry_id': task['round_entry_id'],
                       'task_id': resp_dict['data'][0]['id'],
                       'rating': '1.0'}
        data['ratings'].append(bulk_rating)

    entry_id = resp_dict['data'][0]['round_entry_id']
    task_id = resp_dict['data'][0]['id']

    import pdb; pdb.set_trace()

    resp_dict = fetch_json(url_base + '/juror/bulk_submit/rating', data)

    print '.. submitted %s ratings on task #%s' % (len(data), task_id)
    '''

    # TODO:
    #
    # submit random results for all open tasks in a round
    # close out the round the round
    # load the results of this round into the next

    print cookies

    import pdb;pdb.set_trace()


def add_votes(domain, round_id):

    # get all the jurors that have open tasks in a round
    # get juror's tasks
    # submit random valid votes until there are no more tasks

    pass


def main():
    config = utils.load_env_config()

    prs = argparse.ArgumentParser('test the montage server endpoints')
    add_arg = prs.add_argument
    add_arg('--remote', action="store_true", default=False)

    args = prs.parse_args()

    if args.remote:
        url_base = 'https://tools.wmflabs.org/montage-dev'
    else:
        url_base = 'http://localhost:5000'

    parsed_url = urlparse.urlparse(url_base)

    domain = parsed_url.netloc.partition(':')[0]
    if domain.startswith('localhost'):
        domain = 'localhost.local'
        ck_val = config['dev_local_cookie_value']
    else:
        ck_val = config['dev_remote_cookie_value']

    ck = cookielib.Cookie(version=0, name='clastic_cookie',
                          value=ck_val,
                          port=None, port_specified=False,
                          domain=domain, domain_specified=True,
                          domain_initial_dot=False,
                          path=parsed_url.path, path_specified=True,
                          secure=False, expires=None, discard=False,
                          comment=None, comment_url=None, rest={},
                          rfc2109=False)
    cookies.set_cookie(ck)

    full_run(url_base, remote=args.remote)


if __name__ == '__main__':
    main()
