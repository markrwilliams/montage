# -*- coding: utf-8 -*-

# Relational database models for Montage
import random
import datetime
import itertools
from collections import Counter, defaultdict
from math import ceil

from sqlalchemy import (Text,
                        Column,
                        String,
                        Integer,
                        Float,
                        Boolean,
                        DateTime,
                        TIMESTAMP,
                        ForeignKey)
from sqlalchemy.sql import func
from sqlalchemy.orm import relationship, joinedload
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.ext.associationproxy import association_proxy

from boltons.strutils import slugify
from boltons.iterutils import chunked, first, unique_iter
from boltons.statsutils import mean

from utils import (format_date,
                   to_unicode,
                   get_mw_userid,
                   weighted_choice,
                   PermissionDenied, DoesNotExist, InvalidAction)
from imgutils import make_mw_img_url
from loaders import get_entries_from_gist_csv, load_category
from simple_serdes import DictableBase, JSONEncodedDict

Base = declarative_base(cls=DictableBase)


# Some basic display settings for now
DEFAULT_ROUND_CONFIG = {'show_link': True,
                        'show_filename': True,
                        'show_resolution': True}

ONE_MEGAPIXEL = 1e6
DEFAULT_MIN_RESOLUTION = 2 * ONE_MEGAPIXEL
IMPORT_CHUNK_SIZE = 200


"""
Column ordering and groupings:
* ID
* Data
* Metadata (creation date)
* 1-n relationships
* n-n relationships
"""

"""
# Note on "flags"

The "flags" column, when present, is a string
column with serialized JSON data, used for incidental data that isn't
represented in columns (and thus can't be indexed/queried
directly). The hope is that this saves us a migration or two down the
line.

Also note that unless there is an __init__ that populates the field
with a dict, brand new flags-having objects will have None for the
flags attribute.
"""

MAINTAINERS = ['MahmoudHashemi', 'Slaporte', 'Yarl']


"""
class RDBSession(sessionmaker()):
    def __init__(self, *a, **kw):
        super(RDBSession, self).__init__(*a, **kw)
        self.dao_list = []
"""


class User(Base):
    __tablename__ = 'users'

    id = Column(Integer, primary_key=True)

    username = Column(String(255))
    is_organizer = Column(Boolean, default=False)
    last_active_date = Column(DateTime)

    create_date = Column(TIMESTAMP, server_default=func.now())
    flags = Column(JSONEncodedDict)

    created_by = Column(Integer, ForeignKey('users.id'))
    coordinated_campaigns = relationship('CampaignCoord', back_populates='user')
    campaigns = association_proxy('coordinated_campaigns', 'campaign',
                                  creator=lambda c: CampaignCoord(campaign=c))

    jurored_rounds = relationship('RoundJuror', back_populates='user')
    rounds = association_proxy('jurored_rounds', 'round',
                               creator=lambda r: RoundJuror(round=r))

    tasks = relationship('Task', back_populates='user')
    # update_date?

    def __init__(self, **kw):
        self.flags = kw.pop('flags', {})
        super(User, self).__init__(**kw)

    @property
    def is_maintainer(self):
        return self.username in MAINTAINERS

    def to_info_dict(self):
        ret = {'id': self.id,
               'username': self.username,
               'is_organizer': self.is_organizer,
               'is_maintainer': self.is_maintainer}
        return ret

    def to_details_dict(self):
        ret = self.to_info_dict()
        ret['last_active_date'] = self.last_active_date
        ret['created_by'] = self.created_by
        return ret


class Campaign(Base):
    __tablename__ = 'campaigns'

    id = Column(Integer, primary_key=True)
    name = Column(String(255))

    # open/close can be used to select/verify that images were
    # actually uploaded during the contest window
    open_date = Column(DateTime)
    close_date = Column(DateTime)

    create_date = Column(TIMESTAMP, server_default=func.now())
    flags = Column(JSONEncodedDict)

    rounds = relationship('Round', back_populates='campaign')
    campaign_coords = relationship('CampaignCoord')
    coords = association_proxy('campaign_coords', 'user',
                               creator=lambda user: CampaignCoord(coord=user))

    @property
    def active_round(self):
        return first([r for r in self.rounds
                      if r.status in ('active', 'paused')], None)

    def to_info_dict(self):
        ret = {'id': self.id,
               'name': self.name,
               'url_name': slugify(self.name, '-')}
        return ret

    def to_details_dict(self, admin=None):  # TODO: with_admin?
        ret = self.to_info_dict()
        ret['rounds'] = [rnd.to_info_dict() for rnd in self.rounds]
        ret['coordinators'] = [user.to_info_dict() for user in self.coords]
        active_rnd = self.active_round
        ret['active_round'] = active_rnd.to_info_dict() if active_rnd else None
        return ret


class CampaignCoord(Base):  # Coordinator, not Coordinate
    __tablename__ = 'campaign_coords'

    user_id = Column(Integer, ForeignKey('users.id'), primary_key=True)
    campaign_id = Column(Integer, ForeignKey('campaigns.id'), primary_key=True)

    user = relationship('User', back_populates='coordinated_campaigns')
    campaign = relationship('Campaign', back_populates='campaign_coords')

    def __init__(self, campaign=None, coord=None):
        if campaign is not None:
            self.campaign = campaign
        self.user = coord


class Round(Base):
    """The "directions" field is for coordinators to communicate
    localized directions to jurors, whereas the "description" field is
    for coordinator comments (and not shown to jurors).
    """
    __tablename__ = 'rounds'

    id = Column(Integer, primary_key=True)
    name = Column(String(255))
    description = Column(Text)
    directions = Column(Text)
    open_date = Column(DateTime)
    close_date = Column(DateTime)
    status = Column(String(255))
    vote_method = Column(String(255))
    quorum = Column(Integer)

    config = Column(JSONEncodedDict, default=DEFAULT_ROUND_CONFIG)
    deadline_date = Column(TIMESTAMP)

    create_date = Column(TIMESTAMP, server_default=func.now())
    flags = Column(JSONEncodedDict)

    campaign_id = Column(Integer, ForeignKey('campaigns.id'))
    campaign_seq = Column(Integer)

    campaign = relationship('Campaign', back_populates='rounds')

    round_jurors = relationship('RoundJuror')
    jurors = association_proxy('round_jurors', 'user',
                               creator=lambda u: RoundJuror(user=u))

    round_entries = relationship('RoundEntry')
    entries = association_proxy('round_entries', 'entry',
                                creator=lambda e: RoundEntry(entry=e))

    def get_count_map(self):
        # TODO TODO TODO
        # when more info is needed, can get session with
        # inspect(self).session (might be None if not attached), only
        # disadvantage is that user is not available to do permissions
        # checking.
        from sqlalchemy import inspect
        rdb_session = inspect(self).session
        if not rdb_session:
            # TODO: just make a session
            raise RuntimeError('cannot get counts for detached Round')
        rdb_session.query(Task)  # etc.

        """
        interesting counts:
          * entries
          * disqualified entries
          * open tasks
          * total tasks
        """

    def to_info_dict(self):
        ret = {'id': self.id,
               'name': self.name,
               'directions': self.directions,
               'canonical_url_name': slugify(self.name, '-'),
               'vote_method': self.vote_method,
               'open_date': format_date(self.open_date),
               'close_date': format_date(self.close_date),
               'deadline_date': format_date(self.deadline_date),
               'status': self.status}
        return ret

    def to_details_dict(self):
        ret = self.to_info_dict()
        ret['quorum'] = self.quorum
        ret['jurors'] = [rj.to_details_dict() for rj in self.round_jurors]
        return ret


class RoundJuror(Base):
    __tablename__ = 'round_jurors'

    user_id = Column(Integer, ForeignKey('users.id'), primary_key=True)
    round_id = Column(Integer, ForeignKey('rounds.id'), primary_key=True)
    is_active = Column(Boolean, default=True)
    flags = Column(JSONEncodedDict)

    user = relationship('User', back_populates='jurored_rounds')
    round = relationship('Round', back_populates='round_jurors')

    def __init__(self, round=None, user=None):
        if round is not None:
            # lesson: setting round to None would give an error about
            # trying to "blank-out primary key column"
            self.round = round
        if user is not None:
            self.user = user

    def to_details_dict(self):
        ret = {'id': self.user.id,
               'username': self.user.username,
               'is_active': self.is_active}
        if self.flags:
            ret['flags'] = self.flags
        return ret


class Entry(Base):
    # if this is being kept generic for other types of media judging,
    # then I think a "duration" attribute makes sense -mh
    __tablename__ = 'entries'

    id = Column(Integer, primary_key=True)

    # page_id?
    name = Column(String(255), index=True, unique=True)
    mime_major = Column(String(255))
    mime_minor = Column(String(255))
    width = Column(Integer)
    height = Column(Integer)
    resolution = Column(Integer)
    # if we ever figure out how to get the monument ID
    subject_id = Column(String(255))
    upload_user_id = Column(Integer)
    upload_user_text = Column(String(255))
    upload_date = Column(DateTime)

    # TODO: img_sha1/page_touched for updates?
    create_date = Column(TIMESTAMP, server_default=func.now())
    flags = Column(JSONEncodedDict)

    entered_rounds = relationship('RoundEntry')
    rounds = association_proxy('entered_rounds', 'round',
                               creator=lambda r: RoundEntry(round=r))

    def to_info_dict(self):
        return {'id': self.id}

    def to_details_dict(self, **kw):
        with_uploader = kw.pop('with_uploader', None)
        ret = self.to_info_dict()
        ret.update({'upload_date': format_date(self.upload_date),
                    'mime_major': self.mime_major,
                    'mime_minor': self.mime_minor,
                    'name': self.name,
                    'height': self.height,
                    'width': self.width,
                    'url': make_mw_img_url(self.name),
                    'url_sm': make_mw_img_url(self.name, size='small'),
                    'url_med': make_mw_img_url(self.name, size='medium'),
                    'resolution': self.resolution})
        if with_uploader:
            ret['upload_user_text'] = self.upload_user_text
        return ret


class RoundEntry(Base):
    __tablename__ = 'round_entries'

    id = Column(Integer, primary_key=True)
    entry_id = Column(Integer, ForeignKey('entries.id'))
    round_id = Column(Integer, ForeignKey('rounds.id'))

    dq_user_id = Column(Integer, ForeignKey('users.id'))
    dq_reason = Column(String(255))  # in case it's disqualified
    # examples: too low resolution, out of date range
    flags = Column(JSONEncodedDict)

    entry = relationship(Entry, back_populates='entered_rounds')
    round = relationship(Round, back_populates='round_entries')
    tasks = relationship('Task', back_populates='round_entry')
    rating = relationship('Rating', back_populates='round_entry')
    ranking = relationship('Ranking', back_populates='round_entry')

    def __init__(self, entry=None, round=None):
        if entry is not None:
            self.entry = entry
        if round is not None:
            self.round = round
        return


class Rating(Base):
    __tablename__ = 'ratings'

    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey('users.id'))
    task_id = Column(Integer, ForeignKey('tasks.id'))
    round_entry_id = Column(Integer, ForeignKey('round_entries.id'))

    value = Column(Float)

    round_entry = relationship('RoundEntry', back_populates='rating')

    create_date = Column(TIMESTAMP, server_default=func.now())
    flags = Column(JSONEncodedDict)


class Ranking(Base):
    __tablename__ = 'rankings'

    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey('users.id'))
    task_id = Column(Integer, ForeignKey('tasks.id'))
    round_entry_id = Column(Integer, ForeignKey('round_entries.id'))

    value = Column(Integer)

    round_entry = relationship('RoundEntry', back_populates='ranking')

    create_date = Column(TIMESTAMP, server_default=func.now())
    flags = Column(JSONEncodedDict)


class Task(Base):
    __tablename__ = 'tasks'

    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey('users.id'))
    round_entry_id = Column(Integer, ForeignKey('round_entries.id'))

    user = relationship('User', back_populates='tasks')
    round_entry = relationship('RoundEntry', back_populates='tasks')

    create_date = Column(TIMESTAMP, server_default=func.now())
    complete_date = Column(DateTime)
    cancel_date = Column(DateTime)

    entry = association_proxy('round_entry', 'entry',
                              creator=lambda e: RoundEntry(entry=e))

    def to_info_dict(self):
        ret = {'id': self.id,
               'round_entry_id': self.round_entry_id}
        return ret

    def to_details_dict(self):
        ret = self.to_info_dict()
        ret['entry'] = self.entry.to_details_dict()
        return ret


class RoundResultsSummary(Base):
    """# Results modeling

    This is more like a persistent cache. Results could be recomputed from
    the ratings/rankings.

    ## Round results

    All have:

    * Total number in/out
    * Time created/closed
    * Created/closed by

    Style-specific:

    * Rating-based
        * Winning images (up to 50, sampled?)
        * Parameters (scale, threshold)
    * Ranking-based
        * ?

    """
    __tablename__ = 'results_summaries'

    id = Column(Integer, primary_key=True)

    round_id = Column(Integer, ForeignKey('rounds.id'))

    summary = Column(JSONEncodedDict)

    create_date = Column(TIMESTAMP, server_default=func.now())

'''
class CampaignResults(Base):
    """
    (Same as last round results?)

    On the frontend I'd like to see:

    * Ranked winners
    * Total number of entries
    * Total number of votes
    * Summary of each round (with jurors)
    * Organizers and coordinators
    * Dates

    """
    __tablename__ = 'campaign_results'

    id = Column(Integer, primary_key=True)

    campaign_id = Column(Integer, ForeignKey('campaign.id'))

    summary = Column(JSONEncodedDict)

    create_date = Column(TIMESTAMP, server_default=func.now())
'''


class AuditLogEntry(Base):
    __tablename__ = 'audit_log_entries'

    id = Column(Integer, primary_key=True)

    user_id = Column(Integer, ForeignKey('users.id'))
    campaign_id = Column(Integer, ForeignKey('campaigns.id'))
    round_id = Column(Integer, ForeignKey('rounds.id'))
    round_entry_id = Column(Integer, ForeignKey('round_entries.id'))

    role = Column(String(255))
    action = Column(String(255))
    message = Column(Text)

    flags = Column(JSONEncodedDict)

    create_date = Column(TIMESTAMP, server_default=func.now())

    def to_info_dict(self):
        ret = {'id': self.id,
               'user_id': self.user_id,
               'campaign_id': self.campaign_id,
               'round_id': self.round_id,
               'round_entry_id': self.round_entry_id,
               'role': self.role,
               'action': self.action,
               'message': self.message,
               'create_date': format_date(self.create_date)}
        return ret


class UserDAO(object):
    """The Data Acccess Object wraps the rdb_session and active user
    model, providing a layer for model manipulation through
    expressively-named methods.

    As the DAO expands, it will likely break up into multiple DAOs for
    different areas of the schema.

    # TODO: will blow up a bit if user is None
    """
    def __init__(self, rdb_session, user):
        self.rdb_session = rdb_session
        self.user = user

    @property
    def role(self):
        cn_role = self.__class__.__name__.replace('DAO', '').lower()
        # allow overriding with _role attribute
        return getattr(self, '_role', cn_role)

    def query(self, *a, **kw):
        "a call-through to the underlying session.query"
        return self.rdb_session.query(*a, **kw)

    def log_action(self, action, **kw):
        # TODO: file logging here too
        user_id = self.user.id if self.user else None
        round_entry = kw.pop('round_entry', None)
        round_entry_id = kw.pop('round_entry_id',
                                round_entry.id if round_entry else None)

        rnd = kw.pop('round', None)
        round_id = kw.pop('round_id', rnd.id if rnd else None)

        if not round_id and round_entry_id:
            rnd = self.query(RoundEntry).get(round_entry_id).round
            round_id = rnd.id

        campaign = kw.pop('campaign', None)
        campaign_id = kw.pop('campaign_id', campaign.id if campaign else None)

        if round_id and not campaign_id:
            campaign = self.query(Round).get(round_id).campaign
            campaign_id = campaign.id

        cn_role = self.__class__.__name__.replace('DAO', '').lower()
        role = kw.pop('role', cn_role)

        message = kw.pop('message', None)
        flags = dict(kw.pop('flags', {}))

        ale = AuditLogEntry(user_id=user_id,
                            campaign_id=campaign_id,
                            round_id=round_id,
                            round_entry_id=round_entry_id,
                            role=role,
                            action=action,
                            message=message,
                            flags=flags)

        self.rdb_session.add(ale)
        return


class CoordinatorDAO(UserDAO):
    """A Data Access Object for the Coordinator's view"""

    # Some restrictions on editing round properties:
    # (these restrictions are enforced in the endpoint funcs)
    #
    #   - no reassignment required: name, description, directions,
    #     display_settings
    #   - reassignment required: quorum, active_jurors
    #   - not updateable: id, open_date, close_date, vote_method,
    #     campaign_id/seq

    # Read methods
    def get_campaign(self, campaign_id):
        campaign = self.query(Campaign)\
                       .filter(
                           Campaign.coords.any(username=self.user.username))\
                       .filter_by(id=campaign_id)\
                       .one_or_none()
        return campaign

    def get_all_campaigns(self):
        campaigns = self.query(Campaign)\
                        .filter(
                            Campaign.coords.any(username=self.user.username))\
                        .all()
        return campaigns

    def get_round(self, round_id):
        round = self.query(Round)\
                    .filter(
                        Round.campaign.has(
                            Campaign.coords.any(username=self.user.username)))\
                    .filter_by(id=round_id)\
                    .one_or_none()
        return round

    def get_round_task_counts(self, rnd):
        # the fact that these are identical for two DAOs shows it
        # should be on the Round model or somewhere else shared
        re_count = self.query(RoundEntry).filter_by(round_id=rnd.id).count()
        total_tasks = self.query(Task)\
                          .filter(Task.round_entry.has(round_id=rnd.id),
                                  Task.cancel_date == None)\
                          .count()
        total_open_tasks = self.query(Task)\
                               .filter(Task.round_entry.has(round_id=rnd.id),
                                       Task.complete_date == None,
                                       Task.cancel_date == None)\
                               .count()
        if total_tasks:
            percent_open = round((100.0 * total_open_tasks) / total_tasks, 3)
        else:
            percent_open = 0.0
        return {'total_round_entries': re_count,
                'total_tasks': total_tasks,
                'total_open_tasks': total_open_tasks,
                'percent_tasks_open': percent_open}

    def get_entry_name_map(self, filenames):
        entries = self.query(Entry)\
                      .filter(Entry.name.in_(filenames))\
                      .all()
        ret = {}
        for entry in entries:
            name = entry.name
            ret[name] = entry
        return ret

    # write methods
    def edit_campaign(self, campaign_id, campaign_dict):
        ret = self.rdb_session.query(Campaign)\
                              .filter_by(id=campaign_id)\
                              .update(campaign_dict)
        self.rdb_session.commit()

        return ret

    def create_round(self, campaign, name, quorum,
                     vote_method, jurors, deadline_date):
        # TODO:
        # if campaign.active_round:
        #     raise InvalidAction('can only create one active/paused round at a'
        #                         ' time. cancel or complete your existing'
        #                         ' rounds before trying again')

        jurors = [self.get_or_create_user(j, 'juror', campaign=campaign)
                  for j in jurors]
        rnd = Round(name=name,
                    campaign=campaign,
                    campaign_seq=len(campaign.rounds),
                    status='paused',
                    quorum=quorum,
                    deadline_date=deadline_date,
                    vote_method=vote_method,
                    jurors=jurors)

        self.rdb_session.add(rnd)
        self.rdb_session.commit()

        j_names = [j.username for j in jurors]
        msg = ('%s created %s round "%s" (#%s) with jurors %r for'
               ' campaign "%s"' % (self.user.username, vote_method,
                                   rnd.name, rnd.id, j_names, campaign.name))
        self.log_action('create_round', round=rnd, message=msg)

        return rnd

    def autodisqualify_by_date(self, rnd):
        campaign = rnd.campaign
        min_date = campaign.open_date
        max_date = campaign.close_date

        round_entries = self.query(RoundEntry)\
                            .join(Entry)\
                            .filter(RoundEntry.round_id == rnd.id)\
                            .filter((Entry.upload_date < min_date) |
                                    (Entry.upload_date > max_date))\
                            .all()

        for round_entry in round_entries:
            dq_reason = ('upload date %s is out of campaign date range %s - %s'
                         % (round_entry.entry.upload_date, min_date, max_date))
            round_entry.dq_reason = dq_reason
            round_entry.dq_user_id = self.user.id

        msg = ('%s disqualified %s entries outside of date range %s - %s'
               % (self.user.username, len(round_entries), min_date, max_date))
        self.log_action('autodisqualify_by_date', round=rnd, message=msg)

        return round_entries

    def autodisqualify_by_resolution(self, rnd):
        # TODO: get from config
        min_res = rnd.config.get('min_resolution', DEFAULT_MIN_RESOLUTION)

        round_entries = self.query(RoundEntry)\
                            .join(Entry)\
                            .filter(RoundEntry.round_id == rnd.id)\
                            .filter(Entry.resolution < min_res)\
                            .all()

        min_res_str = round(min_res / ONE_MEGAPIXEL, 2)
        for r_ent in round_entries:
            entry_res_str = round(r_ent.entry.resolution / ONE_MEGAPIXEL, 2)
            dq_reason = ('resolution %s is less than %s minimum '
                         % (entry_res_str, min_res_str))
            r_ent.dq_reason = dq_reason
            r_ent.dq_user_id = self.user.id

        msg = ('%s disqualified %s entries smaller than %s megapixels'
               % (self.user.username, len(round_entries), min_res_str))
        self.log_action('autodisqualify_by_resolution', round=rnd, message=msg)

        return round_entries

    def autodisqualify_by_uploader(self,
                                   rnd,
                                   dq_coords=True,
                                   dq_organizers=True,
                                   dq_maintainers=False):
        dq_group = {}
        dq_usernames = [j.username for j in rnd.jurors]
        for username in dq_usernames:
            dq_group[username] = 'juror'

        if dq_coords:
            coord_usernames = [c.username for c in rnd.campaign.coords]
            dq_usernames += coord_usernames
            for username in coord_usernames:
                dq_group[username] = 'coordinator'

        if dq_organizers:
            organizers = self.query(User)\
                             .filter_by(is_organizer=True)\
                             .all()
            organizer_usernames = [u.username for u in organizers]
            dq_usernames += organizer_usernames
            for username in organizer_usernames:
                dq_group[username] = 'organizer'

        if dq_maintainers:
            dq_usernames += MAINTAINERS
            for username in MAINTAINERS:
                dq_group[username] = 'maintainer'

        dq_usernames = set(dq_usernames)

        round_entries = self.query(RoundEntry)\
                            .join(Entry)\
                            .filter(RoundEntry.round_id == rnd.id)\
                            .filter(Entry.upload_user_text.in_(dq_usernames))\
                            .all()

        for round_entry in round_entries:
            upload_user = round_entry.entry.upload_user_text
            dq_reason = 'upload user %s is %s'\
                        % (upload_user, dq_group[upload_user])
            round_entry.dq_reason = dq_reason
            round_entry.dq_user_id = self.user.id

        msg = ('%s disqualified %s entries based on upload user'
               % (self.user.username, len(round_entries)))
        self.log_action('autodisqualify_by_uploader', round=rnd, message=msg)

        return round_entries

    def pause_round(self, rnd):
        rnd.status = 'paused'

        msg = '%s paused round "%s"' % (self.user.username, rnd.name)
        self.log_action('pause_round', round=rnd, message=msg)

    def activate_round(self, rnd):
        if rnd.status != 'paused':
            raise InvalidAction('can only activate round in a paused state,'
                                ' not %r' % (rnd.status,))
        if not rnd.open_date:
            tasks = create_initial_tasks(self.rdb_session, rnd)
            rnd.open_date = datetime.datetime.utcnow()

            msg = '%s opened round %s' % (self.user.username, rnd.name)
            self.log_action('open_round', round=rnd, message=msg)

        rnd.status = 'active'

        msg = '%s activated round "%s"' % (self.user.username, rnd.name)
        self.log_action('activate_round', round=rnd, message=msg)

        return

    def add_entries_from_cat(self, rnd, cat_name):
        entries = load_category(cat_name)

        entries, new_entry_count = self.add_entries(rnd, entries)

        msg = ('%s loaded %s entries from category (%s), %s new entries added'
               % (self.user.username, len(entries), cat_name, new_entry_count))
        self.log_action('add_entries', message=msg, round=rnd)

        return entries

    def add_entries_from_csv_gist(self, rnd, gist_url):
        # NOTE: this no longer creates RoundEntries, use
        # add_round_entries to do this.

        entries = get_entries_from_gist_csv(gist_url)

        entries, new_entry_count = self.add_entries(rnd, entries)

        msg = ('%s loaded %s entries from csv gist (%r), %s new entries added'
               % (self.user.username, len(entries), gist_url, new_entry_count))
        self.log_action('add_entries', message=msg, round=rnd)

        return entries

    def add_entries(self, rnd, entries):
        entry_chunks = chunked(entries, IMPORT_CHUNK_SIZE)
        ret = []
        new_entry_count = 0

        for entry_chunk in entry_chunks:
            entry_names = [to_unicode(e.name) for e in entry_chunk]
            db_entries = self.get_entry_name_map(entry_names)

            for entry in entry_chunk:
                db_entry = db_entries.get(to_unicode(entry.name))
                if db_entry:
                    entry = db_entry
                else:
                    new_entry_count += 1
                    self.rdb_session.add(entry)

                ret.append(entry)

        return ret, new_entry_count

    def add_round_entries(self, rnd, entries, source=''):
        if rnd.status != 'paused':
            raise InvalidAction('round must be paused to add new entries')
        existing_names = set(self.rdb_session.query(Entry.name).
                             join(RoundEntry).
                             filter_by(round=rnd).
                             all())
        new_entries = [e for e
                       in unique_iter(entries, key=lambda e: e.name)
                       if e.name not in existing_names]

        rnd.entries.extend(new_entries)

        msg = ('%s added %s round entries, %s new'
               % (self.user.username, len(entries), len(new_entries)))
        if source:
            msg += ' (from %s)' % (source,)
        self.log_action('add_round_entries', message=msg, round=rnd)

        return new_entries

    def get_or_create_user(self, user, role, **kw):
        # kw is for including round/round_id/campaign/campaign_id
        # which is used in the audit log if the user is created
        if isinstance(user, User):
            return user
        elif not isinstance(user, basestring):
            raise TypeError('expected user or username, not %r' % user)
        username = user
        user = lookup_user(self.rdb_session, username=username)

        if not user:
            creator = self.user
            user_id = get_mw_userid(username)
            user = User(id=user_id,
                        username=username,
                        created_by=creator.id if creator else None)
            self.rdb_session.add(user)
            msg = ('%s created user %s while adding them as a %s'
                   % (creator.username if creator else '', username, role))
            self.log_action('create_user', message=msg, **kw)

        return user

    def cancel_round(self, rnd):
        tasks = self.query(Task)\
                    .filter(Task.round_entry.has(round_id=rnd.id),
                            Task.complete_date == None)\
                    .all()
        cancel_date = datetime.datetime.utcnow()

        rnd.status = 'cancelled'
        rnd.close_date = cancel_date

        for task in tasks:
            task.cancel_date = cancel_date

        msg = '%s cancelled round "%s" and %s open tasks' %\
              (self.user.username, rnd.name, len(tasks))
        self.log_action('cancel_round', round=rnd, message=msg)

        return rnd

    def finalize_rating_round(self, rnd, threshold):
        assert rnd.vote_method in ('rating', 'yesno')
        # TODO: assert all tasks complete

        rnd.close_date = datetime.datetime.utcnow()
        rnd.status = 'finalized'
        rnd.config['final_threshold'] = threshold

        advance_count = len(self.get_rating_advancing_group(rnd, threshold))

        msg = ('%s finalized rating round "%s" at threshold %s,'
               ' with %s entries advancing'
               % (self.user.username, rnd.name, threshold, advance_count))
        self.log_action('finalize_round', round=rnd, message=msg)

        return

    def get_rating_advancing_group(self, rnd, threshold=None):
        assert rnd.vote_method in ('rating', 'yesno')

        if threshold is None:
            threshold = rnd.config.get('final_threshold')
        if threshold is None:
            raise ValueError('expected threshold or finalized round')

        assert 0.0 <= threshold <= 1.0

        avg = func.avg(Rating.value).label('average')

        results = self.query(RoundEntry, Rating, avg)\
                      .options(joinedload('entry'))\
                      .filter_by(round_id=rnd.id)\
                      .join(Rating)\
                      .group_by(Rating.round_entry_id)\
                      .having(avg >= threshold)\
                      .all()

        entries = [res[0].entry for res in results]

        return entries

    def get_round_average_rating_map(self, rnd):
        results = self.query(Rating, func.avg(Rating.value).label('average'))\
                      .join(RoundEntry)\
                      .filter(Rating.round_entry.has(round_id=rnd.id))\
                      .group_by(Rating.round_entry_id)\
                      .all()

        # thresh_counts = get_threshold_map(r[1] for r in ratings)
        rating_ctr = Counter([r[1] for r in results])

        return dict(rating_ctr)

    def modify_jurors(self, rnd, new_jurors):
        # NOTE: this does not add or remove tasks. Contrast this with
        # changing the quorum, which would remove tasks, but carries the
        # issue of possibly having to reweight or discard completed ratings.

        # TODO: check to make sure only certain round actions can happen
        # when paused, others only when active, basically none when the
        # round is complete or cancelled.
        if not getattr(new_jurors, 'username', None):
            new_jurors = [self.get_or_create_user(j, 'juror', round=rnd)
                           for j in new_jurors]

        if rnd.quorum > len(new_jurors):
            raise InvalidAction('expected at least %s jurors to make quorum'
                                ' (%s) for round #%s'
                                % (len(new_jurors), rnd.quorum, rnd.id))
        new_juror_names = sorted([nj.username for nj in new_jurors])
        old_juror_names = sorted([oj.username for oj in rnd.jurors])
        if new_juror_names == old_juror_names:
            raise InvalidAction('new jurors must differ from current jurors')

        res = reassign_tasks(self.rdb_session, rnd, new_jurors)

        for juror in new_jurors:
            if juror.username not in old_juror_names:
                rnd.jurors.append(juror)

        for round_juror in rnd.round_jurors:
            if round_juror.user.username in new_juror_names:
                round_juror.is_active = 1
            else:
                round_juror.is_active = 0

        msg = ('%s changed round #%s jurors (%r -> %r), reassigned %s tasks'
               ' (average juror task queue now at %s)'
               % (self.user.username, rnd.id, old_juror_names, new_juror_names,
                  res['reassigned_task_count'], res['task_count_mean']))

        self.log_action('modify_jurors', round=rnd, message=msg)
        return res

    def create_ranking_tasks(self, rnd, round_entries):
        jurors = rnd.jurors
        ret = []
        for juror in jurors:
            ret.extend([Task(user=juror, round_entry=re)
                        for re in round_entries])
        return ret

    def create_ranking_round(self, campaign, name, jurors, deadline_date):
        # TODO: there might be some cases where they want to jump straight to the final round?
        # TODO: directions, other round params?
        assert campaign.active_round is None
        final_rnds = [r for r in campaign.rounds if r.status == 'finalized']
        prev_finalized_rnd = final_rnds[-1]  # TODO: these are ordered by date?
        assert prev_finalized_rnd.vote_method != 'ranking'

        advancing_group = self.get_rating_advancing_group(prev_finalized_rnd)

        assert 1 < len(advancing_group) <= 40  # TODO: configurable max

        rnd = self.create_round(campaign,
                                name=name,
                                jurors=jurors,
                                quorum=len(jurors),
                                vote_method='ranking',
                                deadline_date=deadline_date)
        source = 'round(#%s)' % prev_finalized_rnd.id
        self.add_round_entries(rnd, advancing_group, source=source)


class OrganizerDAO(CoordinatorDAO):
    def add_coordinator(self, campaign, username):
        user = self.get_or_create_user(username, 'coordinator',
                                       campaign=campaign)
        if user in campaign.coords:
            raise InvalidAction('user is already a coordinator')
        campaign.coords.append(user)
        self.rdb_session.add(user)
        self.rdb_session.commit()

        msg = ('%s added %s as a coordinator of campaign "%s"'
               % (self.user.username, user.username, campaign.name))
        self.log_action('add_coordinator', campaign_id=campaign.id,
                        message=msg)
        return user

    def create_campaign(self, name, open_date, close_date, coords=None):
        # TODO: Check if campaign with this name already exists?
        if not coords:
            coords = [self.user]

        campaign = Campaign(name=name,
                            open_date=open_date,
                            close_date=close_date,
                            coords=coords)

        self.rdb_session.add(campaign)
        self.rdb_session.commit()

        msg = '%s created campaign "%s"' % (self.user.username, campaign.name)
        self.log_action('create_campaign', campaign=campaign, message=msg)

        return campaign

    # Read methods
    def get_all_campaigns(self):
        # Organizers can see everything, including rounds with which
        # they are not connected
        pass


class MaintainerDAO(OrganizerDAO):
    def get_audit_log(self, limit=100, offset=0):
        audit_logs = self.query(AuditLogEntry)\
                         .order_by(AuditLogEntry.create_date.desc())\
                         .limit(limit)\
                         .offset(offset)\
                         .all()
        return audit_logs

    def add_organizer(self, username):
        user = self.get_or_create_user(username, 'organizer')
        if user.is_organizer:
            #raise Exception('organizer already exists')
            pass
        user.is_organizer = True
        self.rdb_session.add(user)
        self.rdb_session.commit()
        return user


def bootstrap_maintainers(rdb_session):
    # returns created users
    ret = []
    for username in MAINTAINERS:
        user = lookup_user(rdb_session, username=username)

        if not user:
            user_id = get_mw_userid(username)
            user = User(id=user_id,
                        username=username,
                        created_by=None)
            rdb_session.add(user)
            ret.append(user)
    return ret



class JurorDAO(UserDAO):
    """A Data Access Object for the Juror's view"""
    # Read methods
    def get_all_rounds(self):
        rounds = self.query(Round)\
                     .filter(Round.jurors.any(username=self.user.username))\
                     .all()
        return rounds

    def get_campaign(self, campaign_id):
        campaign = self.query(Campaign)\
                       .filter(Campaign.rounds.any(
                           Round.jurors.any(username=self.user.username)))\
                       .filter_by(id=campaign_id)\
                       .one_or_none()
        return campaign

    def get_round(self, round_id):
        round = self.query(Round)\
                    .filter(
                        Round.jurors.any(username=self.user.username),
                        Round.id == round_id)\
                    .one_or_none()
        return round

    def get_task(self, task_id):
        task = self.query(Task)\
                   .filter_by(id=task_id)\
                   .one_or_none()
        return task

    def get_tasks(self, num=1, offset=0):
        tasks = self.query(Task)\
                    .filter(Task.user == self.user,
                            Task.complete_date == None)\
                    .limit(num)\
                    .offset(offset)\
                    .all()
        return tasks

    def get_tasks_by_id(self, task_ids):
        if isinstance(task_ids, int):
            task_ids = [task_ids]

        ret = (self.query(Task)
               .options(joinedload('round_entry'))
               .filter(Task.id.in_(task_ids),
                       Task.user == self.user)
               .all())
        return ret

    def get_tasks_from_round(self, rnd, num=1, offset=0):
        tasks = self.query(Task)\
                    .filter(Task.user == self.user,
                            Task.complete_date == None,
                            Task.round_entry.has(round_id=rnd.id))\
                    .limit(num)\
                    .offset(offset)\
                    .all()
        return tasks

    def get_round_task_counts(self, rnd):
        re_count = self.query(RoundEntry).filter_by(round_id=rnd.id).count()
        total_tasks = self.query(Task)\
                          .filter(Task.round_entry.has(round_id=rnd.id),
                                  Task.user_id == self.user.id,
                                  Task.cancel_date == None)\
                          .count()
        total_open_tasks = self.query(Task)\
                               .filter(Task.round_entry.has(round_id=rnd.id),
                                       Task.user_id == self.user.id,
                                       Task.complete_date == None,
                                       Task.cancel_date == None)\
                               .count()
        if total_tasks:
            percent_open = round((100.0 * total_open_tasks) / total_tasks, 3)
        else:
            percent_open = 0.0
        return {'total_round_entries': re_count,
                'total_tasks': total_tasks,
                'total_open_tasks': total_open_tasks,
                'percent_tasks_open': percent_open}

    def apply_rating(self, task, rating):
        if not task.user == self.user:
            # belt and suspenders until server test covers the cross
            # complete case
            raise PermissionDenied()
        rating = Rating(user_id=self.user.id,
                        task_id=task.id,
                        round_entry_id=task.round_entry_id,
                        value=rating)
        self.rdb_session.add(rating)
        task.complete_date = datetime.datetime.utcnow()
        return

    def apply_ranking(self, ranked_tasks):
        """format: [(task1,),
                 (task3,),
                 (task4, task6),
                 (task5,)]

        with task1 being the highest rank. this format is designed to
        support ties.
        """
        pass


def lookup_user(rdb_session, username):
    user = rdb_session.query(User).filter_by(username=username).one_or_none()
    return user


def create_initial_tasks(rdb_session, rnd):
    """this creates the initial tasks.

    there may well be a separate function for reassignment which reads
    from the incomplete Tasks table (that will have to ensure not to
    assign a rating which has already been completed by the same
    juror)
    """
    # TODO: deny quorum > number of jurors
    ret = []

    quorum = rnd.quorum
    jurors = [rj.user for rj in rnd.round_jurors if rj.is_active]
    if not jurors:
        raise InvalidAction('expected round with active jurors')
    random.shuffle(jurors)

    rdb_type = rdb_session.bind.dialect.name

    if rdb_type == 'mysql':
        rand_func = func.rand()
    else:
        rand_func = func.random()

    # this does the shuffling in the database
    shuffled_entries = rdb_session.query(RoundEntry)\
                                  .filter(RoundEntry.round_id == rnd.id,
                                          RoundEntry.dq_user_id == None)\
                                  .order_by(rand_func).all()

    to_process = itertools.chain.from_iterable([shuffled_entries] * quorum)
    # some pictures may get more than quorum votes
    # it's either that or some get less
    per_juror = int(ceil(len(shuffled_entries) * (float(quorum) / len(jurors))))

    juror_iters = itertools.chain.from_iterable([itertools.repeat(j, per_juror)
                                                 for j in jurors])

    pairs = itertools.izip_longest(to_process, juror_iters, fillvalue=None)
    for entry, juror in pairs:
        assert juror is not None, 'should never run out of jurors first'
        if entry is None:
            break

        # TODO: bulk_save_objects
        task = Task(user=juror, round_entry=entry)
        ret.append(task)

    return ret


def reassign_tasks(session, rnd, new_jurors):
    """Different strategies for different outcomes:

    1. Try to balance toward everyone having cast roughly the same
       number of votes (fair)
    2. Try to balance toward the most even task queue length per user
       (fast)
    3. Balance toward the users that have been voting most (fastest)

    This function takes the second approach to get a reasonably fast,
    fair result.

    This function does not create or delete tasks. There must be
    enough new jurors to fulfill round quorum.

    Features:

    * No juror is assigned the same RoundEntry twice.
    * Looks at all Jurors who have ever submitted a Rating for the
      Round, meaning that a Juror can be added, removed, and added
      again without seeing duplicates.
    * Evens out work queues, so that workload can be redistributed.

    """
    # TODO: have this cancel tasks and create new ones.

    assert len(new_jurors) >= rnd.quorum

    all_jurors = session.query(User)\
                        .join(Rating, RoundEntry)\
                        .filter_by(round_id=rnd.id)\
                        .all()
    cur_tasks = session.query(Task)\
                       .options(joinedload('round_entry'))\
                       .join(RoundEntry)\
                       .filter_by(round=rnd)\
                       .all()

    incomp_tasks = []
    reassg_tasks = []

    elig_map = defaultdict(lambda: list(new_jurors))
    work_map = defaultdict(list)

    comp_tasks = [t for t in cur_tasks if t.complete_date]
    for task in comp_tasks:
        try:
            elig_map[task.round_entry].remove(task.user)
        except ValueError:
            pass

    incomp_tasks = [t for t in cur_tasks
                    if not (t.complete_date or t.cancel_date)]

    for task in incomp_tasks:
        work_map[task.user].append(task)

    target_work_map = dict([(j, []) for j in new_jurors])
    target_workload = int(len(incomp_tasks) / float(len(new_jurors))) + 1
    for user, user_tasks in work_map.items():
        if user not in new_jurors:
            reassg_tasks.extend(user_tasks)
            continue

        reassg_tasks.extend(user_tasks[target_workload:])
        target_work_map[user] = user_tasks[:target_workload]
        for task in target_work_map[user]:
            elig_map[task.round_entry].remove(user)

    # and now the distribution of tasks begins

    # future optimization note: totally new jurors are easy, as we can
    # skip eligibility checks

    # assuming initial task randomization remains sufficient here

    reassg_queue = list(reassg_tasks)

    def choose_eligible(eligible_users):
        # TODO: cache?
        wcp = [(target_workload - len(w), u)
               for u, w in target_work_map.items()
               if u in eligible_users]
        wcp = [(p[0] if p[0] > 0 else 0.001, p[1]) for p in wcp]

        return weighted_choice(wcp)

    while reassg_queue:
        task = reassg_queue.pop()
        task.user = choose_eligible(elig_map[task.round_entry])
        target_work_map[task.user].append(task)

    task_count_map = dict([(u, len(t)) for u, t in target_work_map.items()])
    return {'incomplete_task_count': len(incomp_tasks),
            'reassigned_task_count': len(reassg_tasks),
            'task_count_map': task_count_map,
            'task_count_mean': mean(task_count_map.values())}


def make_rdb_session(echo=True):
    from utils import load_env_config
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    try:
        config = load_env_config()
    except Exception:
        print '!!  no db_url specified and could not load config file'
        raise
    else:
        db_url = config.get('db_url')

    engine = create_engine(db_url, echo=echo, encoding='utf8')

    session_type = sessionmaker()
    session_type.configure(bind=engine)
    session = session_type()

    return session


"""* Indexes
* db session management, engine creation, and schema creation separation
* prod db pw management
* add simple_serdes for E-Z APIs

TODO: what should the tallying for ratings look like? Get all the
ratings that form the quorum and average them? or median? (sum is the
same as average) what about when images have more than quorum ratings?

"""
