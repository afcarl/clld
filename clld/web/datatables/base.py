"""
This module provides base classes to compose DataTables, i.e. objects which have a double
nature: On the client they provide the information to instantiate a jquery DataTables
object. Server side they know how to provide the data to the client-side table.
"""
from json import dumps
import re
from copy import copy

from sqlalchemy import asc, desc, or_
from sqlalchemy.types import String, Unicode, Float, Integer
from sqlalchemy.sql.expression import operator
from pyramid.renderers import render
from markupsafe import Markup
from zope.interface import implementer

from clld.db.meta import DBSession
from clld.web.util.htmllib import HTML
from clld.web.util.helpers import link, button, icon
from clld.interfaces import IDataTable


OPERATOR_PATTERN = re.compile('\s*(?P<op>\>\=?|\<\=?|\=\=?)\s*')


def filter_number(col, qs, type_=None):
    op = col.__eq__
    match = OPERATOR_PATTERN.match(qs)
    if match:
        op = {
            '>': col.__gt__,
            '>=': col.__ge__,
            '=': col.__eq__,
            '==': col.__eq__,
            '<': col.__lt__,
            '<=': col.__le__,
        }.get(match.group('op'), col.__eq__)
        qs = qs[match.end():]
    try:
        if type_:
            qs = type_(qs.strip())
        else:
            if isinstance(col.property.columns[0].type, Float):
                qs = float(qs.strip())
            if isinstance(col.property.columns[0].type, Integer):
                qs = int(qs.strip())
        return op(qs)
    except ValueError:
        pass


class Col(object):
    """DataTables are basically a list of column specifications.
    """
    dt_name_pattern = re.compile('[a-z]+[A-Z]+[a-z]+')

    def __init__(self, dt, name, **kw):
        self.dt = dt
        self.name = name
        self.js_args = {'sName': name, 'sTitle': self.dt.req.translate('dt-' + name)}

        for key, val in kw.items():
            if self.dt_name_pattern.match(key):
                self.js_args[key] = val
            else:
                setattr(self, key, val)

        if not hasattr(self, 'model_col'):
            self.model_col = None
            model_col = getattr(self.dt.model, self.name, None)
            if model_col and hasattr(model_col.property, 'columns'):
                self.model_col = model_col

    def order(self):
        return self.model_col

    def search(self, qs):
        if self.model_col:
            if isinstance(self.model_col.property.columns[0].type, (String, Unicode)):
                return self.model_col.contains(qs)
            if isinstance(self.model_col.property.columns[0].type, (Float, Integer)):
                return filter_number(self.model_col, qs)

    def format(self, item):
        return getattr(item, self.name, None) or ''


class LinkCol(Col):
    """Column which renders a link.
    """
    def get_attrs(self, item):
        return {}

    def get_obj(self, item):
        return item

    def format(self, item):
        return link(self.dt.req, self.get_obj(item), **self.get_attrs(item))


class LinkToMapCol(Col):
    def __init__(self, dt, name=None, **kw):
        kw.setdefault('bSearchable', False)
        kw.setdefault('bSortable', False)
        Col.__init__(self, dt, name or '', **kw)

    def format(self, item):
        return button(
            icon('icon-globe'),
            title='show %s on map' % getattr(item, 'name', ''),
            onclick='CLLD.Map.showInfoWindow("id", "%s")' % item.id,
        )


class DetailsRowLinkCol(Col):
    def __init__(self, dt, name=None, route_name=None, **kw):
        kw.setdefault('bSortable', False)
        kw.setdefault('bSearchable', False)
        kw.setdefault('sClass', 'center')
        kw.setdefault('sType', 'html')
        name = name or ''
        self.route_name = route_name or name
        if not self.route_name.endswith('_alt'):
            self.route_name = self.route_name + '_alt'
        Col.__init__(self, dt, name, **kw)

    def format(self, item):
        return button(
            icon('info-sign', inverted=True),
            href=self.dt.req.route_url(self.route_name, ext='snippet.html', id=item.id),
            title="show details",
            class_="btn-info details")


@implementer(IDataTable)
class DataTable(object):
    search = True

    def __init__(self, req, model, eid=None):
        self.model = model
        self.req = req
        self.eid = eid or self.__class__.__name__
        self._cols = None
        self._options = None
        self.count_all = None
        self.count_filtered = None
        self.server_side = False

    def col_defs(self):
        return [LinkCol(self, 'name')]

    @property
    def cols(self):
        if not self._cols:
            self._cols = self.col_defs()
        return self._cols

    @property
    def options(self):
        if not self._options:
            self._options = self.get_options()
            if self._options.get('bServerSide'):
                self.server_side = True
                self._options['bProcessing'] = True
                if 'sAjaxSource' not in self._options:
                    self._options['sAjaxSource'] = self.req.url
        return self._options

    def base_query(self, query):
        return query

    def render(self):
        return Markup(render(
            'clld:web/templates/datatable.mako',
            {'datatable': self, 'options': Markup(dumps(self.options))},
            request=self.req))

    def get_query(self, limit=200, offset=0):
        query = self.base_query(DBSession.query(self.model))
        self.count_all = query.count()

        if self.search:
            for name, val in self.req.params.items():
                if val and name.startswith('sSearch_'):
                    try:
                        clause = self.cols[int(name.split('_')[1])].search(val)
                    except ValueError:
                        clause = None
                    if clause is not None:
                        query = query.filter(clause)

        self.count_filtered = query.count()

        for index in range(int(self.req.params.get('iSortingCols', 0))):
            col = self.cols[int(self.req.params['iSortCol_%s' % index])]
            if col.js_args.get('bSortable', True):
                orders = col.order()
                if orders is not None:
                    if not isinstance(orders, (tuple, list)):
                        orders = [orders]
                    for order in orders:
                        if self.req.params['sSortDir_%s' % index] == 'desc':
                            order = desc(order)
                        query = query.order_by(order)

        query = query.order_by(self.model.pk)
        query = query\
            .limit(int(self.req.params.get('iDisplayLength', limit)))\
            .offset(int(self.req.params.get('iDisplayStart', offset)))
        return query

    def toolbar(self):
        """
        """
        return HTML.div(
            HTML.a(
                icon('download-alt'),
                HTML.span(class_="caret"),
                **{
                    'class_': "btn dropdown-toggle",
                    'data-toggle': "dropdown",
                    'href': "#",
                }
            ),
            HTML.ul(
                HTML.li(HTML.a('csv', href="#")),
                class_="dropdown-menu",
            ),
            button(icon('info-sign', inverted=True), class_='btn-info', id='cdOpener'),
            class_='btn-group right')

    def get_options(self):
        return {
            "bStateSave": True,
            "sDom": "<'row-fluid'<'span6'l><'span6'f<'dt-toolbar'>>r>t<'row-fluid'<'span6'i><'span6'p>>",
            "bAutoWidth": False,
            "sPaginationType": "bootstrap",
            "bServerSide": True,
            "aoColumns": [col.js_args for col in self.cols],
            "iDisplayLength": 25,
            "aLengthMenu": [[25, 50, 100], [25, 50, 100]],
        }