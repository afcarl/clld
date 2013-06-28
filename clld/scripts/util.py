import os
import sys
from distutils.util import strtobool
from collections import defaultdict
import argparse
from urllib import quote_plus
import json
import logging
from functools import partial

import transaction
from sqlalchemy import engine_from_config, create_engine, Integer
from sqlalchemy.sql.expression import cast
from sqlalchemy.orm import joinedload
from path import path
from pyramid.paster import get_appsettings, setup_logging, bootstrap
import requests

from clld.db.meta import DBSession, Base
from clld.db.models import common
from clld.util import slug


def confirm(question, default=False):
    """Ask a yes/no question via raw_input() and return their answer.

    "question" is a string that is presented to the user.
    """
    while True:
        sys.stdout.write(question + (" [Y|n] " if default else " [y|N] "))
        choice = raw_input().lower()
        if not choice:
            return default
        try:
            return strtobool(choice)
        except ValueError:
            sys.stdout.write(
                "Please respond with 'yes' or 'no' (or 'y' or 'n').\n")


def data_file(module, *comps):
    return path(module.__file__).dirname().joinpath('..', 'data', *comps)


def setup_session(config_uri, session=None, base=None, engine=None):
    session = session or DBSession
    base = base or Base
    setup_logging(config_uri)
    settings = get_appsettings(config_uri)
    engine = engine or engine_from_config(settings, 'sqlalchemy.')
    session.configure(bind=engine)
    base.metadata.create_all(engine)
    return path(config_uri.split('#')[0]).abspath().dirname().basename()


class ExistingDir(argparse.Action):
    def __call__(self, parser, namespace, values, option_string=None):
        path_ = path(values)
        if not path_.exists():
            raise argparse.ArgumentError(self, 'path does not exist')
        if not path_.isdir():
            raise argparse.ArgumentError(self, 'path is no directory')
        setattr(namespace, self.dest, path_)


class ExistingConfig(argparse.Action):
    def __call__(self, parser, namespace, values, option_string=None):
        path_ = path(values.split('#')[0])
        if not path_.exists():
            raise argparse.ArgumentError(self, 'file does not exist')
        setattr(namespace, self.dest, values)


class SqliteDb(argparse.Action):
    def __call__(self, parser, namespace, values, option_string=None):
        setattr(namespace, 'engine', create_engine('sqlite:///%s' % values[0]))


def parsed_args(*arg_specs, **kw):
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "config_uri", action=ExistingConfig, help="ini file providing app config")
    parser.add_argument("--module", default=None)
    parser.add_argument(
        "--sqlite", nargs=1, action=SqliteDb, help="sqlite db file")
    for args, _kw in arg_specs:
        parser.add_argument(*args, **_kw)
    args = parser.parse_args()
    engine = getattr(args, 'engine', kw.get('engine', None))
    args.env = bootstrap(args.config_uri) if kw.get('bootstrap', False) else {}
    module = setup_session(
        args.config_uri, session=kw.get('session'), base=kw.get('base'), engine=engine)
    args.module = __import__(args.module or module)
    args.log = logging.getLogger(args.module.__name__)
    if engine:
        args.log.info('using bind %s' % engine)
    args.data_file = partial(data_file, args.module)
    args.module_dir = path(args.module.__file__).dirname()
    return args


def initializedb(create=None, prime_cache=None, **kw):
    args = parsed_args((("--prime-cache-only",), dict(action="store_true")), **kw)
    if not args.prime_cache_only:
        if create:
            create()
    if prime_cache:
        prime_cache()


def gbs(**kw):
    def words(s):
        return set(slug(s.strip(), remove_whitespace=False).split())

    api_url = "https://www.googleapis.com/books/v1/volumes?"

    args = parsed_args(
        (("command",), dict()),
        (("--api-key",), dict(default=kw.get('key', os.environ.get('GBS_API_KEY')))),
        **kw)
    if args.command == 'download' and not args.api_key:
        raise argparse.ArgumentError(None, 'no API key found for download')

    log = args.log
    count = 0

    sources = kw.get('sources')
    if not sources:
        sources = DBSession.query(common.Source)\
            .order_by(cast(common.Source.id, Integer))\
            .options(joinedload(common.Source.data))
    if callable(sources):
        sources = sources()

    with transaction.manager:
        for i, source in enumerate(sources):
            filepath = args.data_file('gbs', 'source%s.json' % source.id)

            if args.command == 'update':
                source.google_book_search_id = None
                source.update_jsondata(gbs={})

            if args.command in ['verify', 'update']:
                if filepath.exists():
                    with open(filepath) as fp:
                        try:
                            data = json.load(fp)
                        except ValueError:
                            log.warn('no JSON object found in: %s' % filepath)
                            continue
                    if not data['totalItems']:
                        continue
                    item = data['items'][0]
                else:
                    continue

            if args.command == 'verify':
                stitle = source.title or source.booktitle
                needs_check = False
                year = item['volumeInfo'].get('publishedDate', '').split('-')[0]
                if not year or year != str(source.year):
                    needs_check = True
                twords = words(stitle)
                iwords = words(
                    item['volumeInfo']['title'] + ' '
                    + item['volumeInfo'].get('subtitle', ''))
                if twords == iwords \
                        or (len(iwords) > 2 and iwords.issubset(twords))\
                        or (len(twords) > 2 and twords.issubset(iwords)):
                    needs_check = False
                if int(source.id) == 241:
                    log.info('%s' % sorted(list(words(stitle))))
                    log.info('%s' % sorted(list(iwords)))
                if needs_check:
                    log.info('------- %s -> %s' % (source.id, item['volumeInfo'].get('industryIdentifiers')))
                    log.info('%s %s' % (item['volumeInfo']['title'], item['volumeInfo'].get('subtitle', '')))
                    log.info(stitle)
                    log.info(item['volumeInfo'].get('publishedDate'))
                    log.info(source.year)
                    log.info(item['volumeInfo'].get('authors'))
                    log.info(source.author)
                    log.info(item['volumeInfo'].get('publisher'))
                    log.info(source.publisher)
                    if not confirm('Are the records the same?'):
                        log.warn('---- removing ----')
                        with open(filepath, 'w') as fp:
                            json.dump({"totalItems": 0}, fp)
            elif args.command == 'update':
                source.google_book_search_id = item['id']
                source.update_jsondata(gbs=item)
                count += 1
            elif args.command == 'download':
                if count > 990:
                    break

                if source.author and (source.title or source.booktitle):
                    title = source.title or source.booktitle
                    if filepath.exists():
                        continue
                    q = [
                        'inauthor:' + quote_plus(source.author.encode('utf8')),
                        'intitle:' + quote_plus(title.encode('utf8')),
                    ]
                    if source.publisher:
                        q.append('inpublisher:' + quote_plus(
                            source.publisher.encode('utf8')))
                    url = api_url + 'q=%s&key=%s' % ('+'.join(q), args.api_key)
                    count += 1
                    r = requests.get(url, headers={'accept': 'application/json'})
                    log.info('%s - %s' % (r.status_code, url))
                    if r.status_code == 200:
                        with open(filepath, 'w') as fp:
                            fp.write(r.text.encode('utf8'))
                    elif r.status_code == 403:
                        log.warn("limit reached")
                        break
    if args.command == 'update':
        log.info('assigned gbs ids for %s out of %s sources' % (count, i))
    elif args.command == 'download':
        log.info('queried gbs for %s sources' % count)


class Data(defaultdict):
    """Dictionary, serving to store references to new db objects during data imports.

    The values are dictionaries, keyed by the name of the mapper class used to create the
    new objects.
    """
    def __init__(self):
        super(Data, self).__init__(dict)

    def add(self, model, key, **kw):
        if kw.keys() == ['_obj']:
            # if a single keyword parameter _obj is passed, we take it to be the object
            # which should be added to the session.
            new = kw['_obj']
        else:
            new = model(**kw)
        self[model.__mapper__.class_.__name__][key] = new
        DBSession.add(new)
        return new