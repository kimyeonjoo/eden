# -*- coding: utf-8 -*-

""" S3 Reporting Framework

    @copyright: 2011-2012 (c) Sahana Software Foundation
    @license: MIT

    @requires: U{B{I{Python 2.6}} <http://www.python.org>}

    Permission is hereby granted, free of charge, to any person
    obtaining a copy of this software and associated documentation
    files (the "Software"), to deal in the Software without
    restriction, including without limitation the rights to use,
    copy, modify, merge, publish, distribute, sublicense, and/or sell
    copies of the Software, and to permit persons to whom the
    Software is furnished to do so, subject to the following
    conditions:

    The above copyright notice and this permission notice shall be
    included in all copies or substantial portions of the Software.

    THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND,
    EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES
    OF MERCHANTABILITY, FITNESS FOR A PARTICULAR PURPOSE AND
    NONINFRINGEMENT. IN NO EVENT SHALL THE AUTHORS OR COPYRIGHT
    HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER LIABILITY,
    WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING
    FROM, OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR
    OTHER DEALINGS IN THE SOFTWARE.
"""

__all__ = ["S3Cube", "S3Report", "S3ContingencyTable"]

import sys
import datetime

try:
    import json # try stdlib (Python 2.6)
except ImportError:
    try:
        import simplejson as json # try external module
    except:
        import gluon.contrib.simplejson as json # fallback to pure-Python module

from gluon import current
from gluon.html import *
from gluon.sqlhtml import OptionsWidget
from gluon.storage import Storage

from s3resource import S3TypeConverter
from s3crud import S3CRUD
from s3search import S3Search
from s3utils import s3_truncate, s3_has_foreign_key, s3_unicode
from s3validators import IS_INT_AMOUNT, IS_FLOAT_AMOUNT, IS_NUMBER, IS_IN_SET


# =============================================================================

class S3Cube(S3CRUD):
    """ RESTful method for pivot table reports """

    T = current.T
    METHODS = {
        "list": T("List"),
        "count": T("Count"),
        "min": T("Minimum"),
        "max": T("Maximum"),
        "sum": T("Sum"),
        "avg": T("Average"),
        #"std": T("Standard Deviation")
    }

    # -------------------------------------------------------------------------
    def apply_method(self, r, **attr):
        """
            API entry point

            @param r: the S3Request instance
            @param attr: controller attributes for the request
        """

        if r.http in ("GET", "POST"):
            output = self.report(r, **attr)
        else:
            r.error(405, current.manager.ERROR.BAD_METHOD)
        return output

    # -------------------------------------------------------------------------
    def _process_report_options(self, form):
        dupe = form.vars.rows == form.vars.cols
        if dupe:
           form.errors.cols = "Duplicate label selected"

    # -------------------------------------------------------------------------
    def report(self, r, **attr):
        """
            Generate a pivot table report

            @param r: the S3Request instance
            @param attr: controller attributes for the request
        """

        T = current.T
        response = current.response
        session = current.session
        s3 = session.s3

        table = self.table
        tablename = self.tablename

        # Report options  -----------------------------------------------------
        #

        # Get the session options
        session_options = s3.report_options
        if session_options and tablename in session_options:
            session_options = session_options[tablename]
        else:
            session_options = Storage()

        # Get the default options
        report_options = self._config("report_options", Storage())
        if report_options and "defaults" in report_options:
            default_options = report_options["defaults"]
        else:
            default_options = Storage()

        # Get the URL options
        url_options = Storage([(k, v) for k, v in
                               r.get_vars.iteritems() if v])

        # Figure out which set of form values to use
        # POST > GET > session > table defaults > list view
        if r.http == "POST":
            form_values = r.post_vars

            # The totals option is used to turn OFF the totals cols/rows but
            # post vars only contain checkboxes that are enabled and checked.
            if "totals" not in r.post_vars:
                form_values["totals"] = "off"
        elif url_options:
            form_values = url_options
            # Without the _formname the form won't validate
            # we put it in here so that URL query strings don't need to
            if not form_values._formname:
                form_values._formname = "report"
        elif session_options:
            form_values = session_options
        elif default_options:
            form_values = default_options
            # Without the _formname the form won't validate
            # we put it in here so that URL query strings don't need to
            if not form_values._formname:
                form_values._formname = "report"
        else:
            form_values = Storage()

        # Generate the report and resource filter form
        show_form = attr.get("interactive_report", True)
        if show_form:
            # Build the form and prepopulate with values we've got
            form = self._create_form(form_values)

            # Validate the form. This populates form.vars (values) and
            # form.errors (field errors).
            # We only compare to the session if POSTing to prevent cross-site
            # scripting.
            if r.http == "POST" and \
                form.accepts(form_values,
                             session,
                             formname="report",
                             onvalidation=self._process_report_options) or \
                form.accepts(form_values,
                             formname="report",
                             onvalidation=self._process_report_options):

                # The form is valid so save the form values into the session
                if "report_options" not in s3:
                    s3.report_options = Storage()

                s3.report_options[tablename] = Storage([(k, v) for k, v in
                                                        form_values.iteritems() if v])

            # Use the values to generate the query filter
            query, errors = self._process_filter_options(form)

            if not errors:
                self.resource.add_filter(query)
        else:
            form = None

        # Get rows, cols, facts and aggregate
        rows = form_values.get("rows", None)
        cols = form_values.get("cols", None)
        fact = form_values.get("fact", None)
        aggregate = form_values.get("aggregate", "list")
        if not aggregate:
            aggregate = "list"

        # Fall back to list if no dimensions specified
        if not rows and not cols:
            self.method = "list"

        # Show totals?
        show_totals = form_values.get("totals", True)
        if show_totals and str(show_totals).lower() in ("false", "off"):
            show_totals = False
        else:
            show_totals = True

        # Get the layers
        layers = []

        if not fact:
            if "name" in table:
                fact = "name"
            else:
                fact = table._id.name

        if fact:
            if not isinstance(fact, list):
                fact = [fact]
            for f in fact:
                f = f.split(",")
                for l in f:
                    if ":" in l:
                        method, layer = l.split(":", 1)
                    else:
                        method = aggregate
                        layer = l
                    layers.append((layer, method))

        # Apply method --------------------------------------------------------
        #
        _show = "%s hide"
        _hide = "%s"

        resource = self.resource
        representation = r.representation

        if not form.errors and self.method == "report":

            # Generate the report ---------------------------------------------
            #
            try:
                report = S3Report(resource, rows, cols, layers)
            except ImportError:
                msg = T("S3Cube unresolved dependencies")
                e = sys.exc_info()[1]
                if hasattr(e, "message"):
                    e = e.message
                else:
                    e = str(e)
                msg = "%s: %s" % (msg, e)
                r.error(400, msg, next=r.url(vars=[]))
            except:
                raise
                msg = T("Could not generate report")
                e = sys.exc_info()[1]
                if hasattr(e, "message"):
                    e = e.message
                else:
                    e = str(e)
                msg = "%s: %s" % (msg, e)
                r.error(400, msg, next=r.url(vars=[]))

            # Represent the report --------------------------------------------
            #
            if representation in ("html", "iframe"):
                report_data = None
                if not report.empty:
                    items = S3ContingencyTable(report,
                                               show_totals=show_totals,
                                               _id="list",
                                               _class="dataTable display report")
                    report_data = items.report_data
                else:
                    items = self.crud_string(self.tablename, "msg_no_match")

                output = dict(items=items,
                              report_data=report_data)

                # Other output options ----------------------------------------
                #
                s3 = response.s3
                s3.dataTable_iDisplayLength = 50
                s3.no_formats = True
                s3.no_sspag = True
                if r.http == "GET":
                    _show = "%s"
                    _hide = "%s hide"
                s3.actions = []
                output.update(sortby=[[0,'asc']])

            else:
                # @todo: support other formats
                r.error(501, current.manager.ERROR.BAD_FORMAT)

        elif representation in ("html", "iframe"):

                # Fallback to list view ---------------------------------------
                #
                current.s3db.configure(self.tablename, insertable=False)
                output = self.select(r, **attr)
                response.s3.actions = [
                        dict(url=r.url(method="", id="[id]", vars=r.get_vars),
                             _class="action-btn",
                             label = str(T("Details")))
                ]
        else:
            r.error(501, current.manager.ERROR.BAD_METHOD)

        # Complete the page ---------------------------------------------------
        #
        if representation in ("html", "iframe"):
            crud_string = self.crud_string
            title = crud_string(self.tablename, "title_report")
            if not title:
                title = crud_string(self.tablename, "title_list")

            if form is not None:
                form = DIV(
                    DIV(form,
                        _id="reportform"
                    ),
                    _style="margin-bottom: 5px;"
                )
            else:
                form = ""

            output["title"] = title
            output["form"] = form
            response.view = self._view(r, "report.html")

        return output

    # -------------------------------------------------------------------------
    def _create_form(self, form_values=None):
        """ Creates the report filter and options form """

        T = current.T
        request = current.request
        resource = self.resource

        # Get list_fields
        _config = self._config
        list_fields = _config("list_fields")
        if not list_fields:
            list_fields = [f.name for f in resource.readable_fields()]

        report_options = _config("report_options", Storage())
        report_rows = report_options.get("rows", list_fields)
        report_cols = report_options.get("cols", list_fields)
        report_fact = report_options.get("facts", list_fields)

        _select_field = self._select_field
        select_rows = _select_field(report_rows,
                                    _id="report-rows",
                                    _name="rows",
                                    form_values=form_values)
        select_cols = _select_field(report_cols,
                                    _id="report-cols",
                                    _name="cols",
                                    form_values=form_values)
        select_fact = _select_field(report_fact,
                                    _id="report-fact",
                                    _name="fact",
                                    form_values=form_values)

        # totals are "on" or True by default
        show_totals = True
        if "totals" in form_values:
            show_totals = form_values["totals"]
            if str(show_totals).lower() in ("false", "off"):
                show_totals = False

        show_totals = INPUT(_type="checkbox", _id="report-totals", _name="totals",
                            value=show_totals)

        methods = report_options.get("methods")
        select_method = self._select_method(methods,
                                            _id="report-aggregate",
                                            _name="aggregate",
                                            form_values=form_values)

        form = FORM()

        # Append filter widgets, if configured
        filter_widgets = self._build_filter_widgets(form_values)
        if filter_widgets:
            form.append(
                FIELDSET(
                    LEGEND("Filter Options ",
                        BUTTON("Show", _type="button", _class="toggle-text", _style="display:none"),
                        BUTTON("Hide", _type="button", _class="toggle-text")
                    ),
                    TABLE(filter_widgets),
                    _id="filter_options"
                )
            )

        # Append report options, always
        form_report_options = FIELDSET(
                LEGEND("Report Options ",
                    BUTTON("Show", _type="button", _class="toggle-text"),
                    BUTTON("Hide", _type="button", _class="toggle-text", _style="display:none")
                ),
                TABLE(
                    TR(
                        TD(LABEL("Rows:", _for="report-rows"), _class="w2p_fl"),
                        TD(select_rows),
                    ),
                    TR(
                        TD(LABEL("Columns:", _for="report-cols"), _class="w2p_fl"),
                        TD(select_cols),
                    ),
                    TR(
                        TD(LABEL("Value:", _for="report-fact"), _class="w2p_fl"),
                        TD(select_fact),
                    ),
                    TR(
                        TD(LABEL("Function for Value:", _for="report-aggregate"), _class="w2p_fl"),
                        TD(select_method),
                    ),
                    TR(
                        TD(LABEL("Show totals:", _for="report-totals"), _class="w2p_fl"),
                        TD(show_totals)
                    ),
                ),
                _id="report_options"
            )
        form.append(form_report_options)
        form.append(INPUT(_value=T("Submit"), _type="submit"))

        return form

    # -------------------------------------------------------------------------
    def _build_filter_widgets(self, form_values=None):
        """
            Builds the filter form widgets
        """

        resource = self.resource

        report_options = self._config("report_options", None)
        if not report_options:
            return None

        filter_widgets = report_options.get("search", None)
        if not filter_widgets:
            return None

        vars = form_values if form_values else self.request.vars
        trows = []
        for widget in filter_widgets:
            name = widget.attr["_name"]
            _widget = widget.widget(resource, vars)
            if not name or _widget is None:
                # Skip this widget as we have nothing but the label
                continue
            label = widget.field
            if isinstance(label, (list, tuple)) and len(label):
                label = label[0]
            comment = ""
            if hasattr(widget, "attr"):
                label = widget.attr.get("label", label)
                comment = widget.attr.get("comment", comment)
            tr = TR(TD("%s: " % label, _class="w2p_fl"),
                    widget.widget(resource, vars))
            if comment:
                tr.append(DIV(DIV(_class="tooltip",
                                  _title="%s|%s" % (label, comment))))
            trows.append(tr)
        return trows

    # -------------------------------------------------------------------------
    def _process_filter_options(self, form):
        """
            Processes the filter widgets into a filter query

            @param form: the filter form

            @rtype: tuple
            @return: A tuple containing (query object, validation errors)
        """

        query = None
        errors = None

        report_options = self._config("report_options", None)
        if not report_options:
            return (None, None)

        filter_widgets = report_options.get("search", None)
        if not filter_widgets:
            return (None, None)

        for widget in filter_widgets:
            name = widget.attr["_name"]
            query, errors = S3Search._build_widget_query(self.resource,
                                                         name,
                                                         widget,
                                                         form,
                                                         query)
            if errors:
                form.errors.update(errors)
        errors = form.errors
        return (query, errors)

    # -------------------------------------------------------------------------
    def _select_field(self, list_fields, form_values=None, **attr):
        """
            Returns a SELECT of field names

            @param list_fields: the fields to include in the options list
            @param attr: the HTML attributes for the SELECT
        """

        name = attr["_name"]
        if form_values:
            value = form_values.get(name, "")
        else:
            value = ""

        table = self.table
        lfields, joins, left, distinct = self.resource.resolve_selectors(list_fields)

        options = []
        for f in lfields:
            if (f.field is None or f.field.name != table._id.name) and f.show:
                options.append((f.selector, f.label))

        dummy_field = Storage(name=name,
                              requires=IS_IN_SET(options))

        return OptionsWidget.widget(dummy_field, value, **attr)

    # -------------------------------------------------------------------------
    def _select_method(self, methods, form_values=None, **attr):
        """
            Returns a SELECT of aggregation methods

            @param methods: list of methods to show
            @param attr: the HTML attributes for the SELECT
        """

        supported_methods = self.METHODS
        if methods:
            methods = [(m, supported_methods[m])
                       for m in methods
                       if m in supported_methods]
        else:
            methods = supported_methods.items()

        name = attr["_name"]

        if form_values:
            value = form_values[name]
        else:
            value = None

        options = []
        for method, label in methods:
            options.append((method, label))

        dummy_field = Storage(name=name,
                              requires=IS_IN_SET(options))

        return OptionsWidget.widget(dummy_field, value, **attr)

    # -------------------------------------------------------------------------
    @staticmethod
    def mname(code):
        """
            Get the method name for a method code, returns None for
            unsupported methods

            @param code: the method code
        """

        methods = S3Cube.METHODS
        T = current.T

        if code is None:
            code = "list"
        if code in methods:
            return T(methods[code])
        else:
            return None

# =============================================================================

class S3Report:
    """ Class representing reports """

    METHODS = ["list", "count", "min", "max", "sum", "avg"] #, "std"]

    def __init__(self, resource, rows, cols, layers):
        """
            Constructor

            @param resource: the S3Resource
            @param rows: the rows dimension
            @param cols: the columns dimension
            @param layers: the report layers as [(fact, aggregate_method)]
        """

        # Initialize ----------------------------------------------------------
        #
        if not rows and not cols:
            raise SyntaxError("No rows or columns for report specified")

        self.resource = resource
        self.rows = rows
        self.cols = cols
        self.layers = layers

        self.records = None
        self.empty = False

        self.lfields = None
        self.dfields = None
        self.rfields = None

        self.row = None
        self.col = None
        self.cell = None

        self.numrows = None
        self.numcols = None
        self.totals = Storage()

        # Get the fields ------------------------------------------------------
        #
        s3db = current.s3db
        # avoid the need to explicitly define report_fields or report_fields 
        # This can be an issue if any of the rows/cols fields are not in list_fields
        # Or list_fields is not defined
        report_options = s3db.get_config(resource.tablename, "report_options")
        if report_options:
            row_column_fields = list(set(report_options.get("rows",[]) + report_options.get("cols",[])))
        else:
            row_column_fields = None
        fields = s3db.get_config(resource.tablename, "report_fields",
                                 row_column_fields)
        self._get_fields(fields=fields)

        # Retrieve the records --------------------------------------------------
        #
        records = resource.sqltable(self.rfields,
                                    as_list=True, start=None, limit=None)
        table = resource.table
        pkey = table._id.name

        # Generate the report -------------------------------------------------
        #
        if records:

            try:
                extract = self._extract
                self.records = Storage([(extract(i, pkey), i) for i in records])
            except KeyError:
                raise KeyError("Could not retrieve primary key values of %s" %
                               resource.tablename)

            # Generate the data frame -----------------------------------------
            #
            from pyvttbl import DataFrame
            df = DataFrame()
            insert = df.insert

            item_list = []
            seen = item_list.append

            flatten = self._flatten
            expand = self._expand

            for row in records:
                item = expand(flatten(row))
                for i in item:
                    if i not in item_list:
                        seen(i)
                        insert(i)

            # Group the items -------------------------------------------------
            #
            rows = self.rows and [self.rows] or []
            cols = self.cols and [self.cols] or []
            pt = df.pivot(pkey, rows, cols, aggregate="tolist")

            # Initialize columns and rows -------------------------------------
            #
            if cols:
                self.col = [Storage({"value": v != "__NONE__" and v or None})
                            for v in [n[0][1] for n in pt.cnames]]
                self.numcols = len(self.col)
            else:
                self.col = [Storage({"value": None})]
                self.numcols = 1

            if rows:
                self.row = [Storage({"value": v != "__NONE__" and v or None})
                            for v in [n[0][1] for n in pt.rnames]]
                self.numrows = len(self.row)
            else:
                self.row = [Storage({"value": None})]
                self.numrows = 1

            # Add the layers --------------------------------------------------
            #
            add_layer = self._add_layer
            layers = list(self.layers)
            for f, m in self.layers:
                add_layer(pt, f, m)
        else:
            # No items to report on -------------------------------------------
            #
            self.empty = True

    # -------------------------------------------------------------------------
    def _get_fields(self, fields=None):
        """
            Determine the fields needed to generate the report

            @param fields: fields to include in the report (all fields)
        """

        resource = self.resource
        table = resource.table

        pkey = table._id.name
        rows = self.rows
        cols = self.cols

        if fields is None:
            fields = []

        # dfields: fields to group the records
        dfields = []
        if rows and rows not in dfields:
            dfields.append(rows)
        if cols and cols not in dfields:
            dfields.append(cols)
        if pkey not in dfields:
            dfields.append(pkey)
        self.dfields = dfields

        # rfields: fields to generate the layers
        rfields = list(fields)
        if rows and rows not in rfields:
            rfields.append(rows)
        if cols and cols not in rfields:
            rfields.append(cols)
        if pkey not in rfields:
            rfields.append(pkey)
        for f, m in self.layers:
            if f not in rfields:
                rfields.append(f)
        self.rfields = rfields

        # lfields: rfields resolved into list fields map
        lfields, joins, left, distinct = resource.resolve_selectors(rfields)
        lfields = Storage([(f.selector, f) for f in lfields])
        self.lfields = lfields

        return

    # -------------------------------------------------------------------------
    def _flatten(self, row):
        """
            Prepare a DAL Row for the data frame

            @param row: the row
        """

        fields = self.dfields
        resource = self.resource
        table = resource.table
        pkey = table._id.name
        item = Storage()
        extract = self._extract
        pk = extract(row, pkey)
        for field in fields:
            value = extract(row, field)
            if value is None and field != pkey:
                value = "__NONE__"
            if type(value) is str:
                value = unicode(value.decode("utf-8"))
            item[field] = value
        item[pkey] = pk
        return item

    # -------------------------------------------------------------------------
    def _extract(self, row, field):
        """
            Extract a field value from a DAL row

            @param row: the row
            @param field: the fieldname (list_fields syntax)
        """

        lfields = self.lfields
        if field not in lfields:
            raise KeyError("Invalid field name: %s" % field)
        lfield = lfields[field]
        tname = lfield.tname
        fname = lfield.fname
        if fname in row:
            value = row[fname]
        elif tname in row and fname in row[tname]:
            value = row[tname][fname]
        else:
            value = None
        return value

    # -------------------------------------------------------------------------
    def _expand(self, row, field=None):
        """
            Expand a data frame row into a list of rows for list:type values

            @param row: the row
            @param field: the field to expand (None for all fields)
        """

        if field is None:
            rows = [row]
            for field in row:
                rows = self._expand(rows, field=field)
            return rows
        else:
            results = []
            rows = row
            for r in rows:
                value = r[field]
                if isinstance(value, (list, tuple)):
                    if not len(value):
                        # Always have at least a None-entry
                        value.append(None)
                    for v in value:
                        result = Storage(r)
                        result[field] = v
                        results.append(result)
                else:
                    results.append(r)
            return results

    # -------------------------------------------------------------------------
    def _add_layer(self, pt, fact, method):
        """
            Compute a new layer from the base layer (pt+items)

            @param pt: the pivot table with record IDs
            @param fact: the fact field for the layer
            @param method: the aggregation method of the layer
        """

        if method not in self.METHODS:
            raise SyntaxError("Unsupported aggregation method: %s" % method)

        items = self.records
        lfields = self.lfields
        rows = self.row
        cols = self.col
        records = self.records
        extract = self._extract
        aggregate = self._aggregate
        resource = self.resource

        RECORDS = "records"
        VALUES = "values"

        table = resource.table
        pkey = table._id.name

        if method is None:
            method = "list"
        layer = (fact, method)

        numcols = len(pt.cnames)
        numrows = len(pt.rnames)

        # Initialize cells
        if self.cell is None:
            self.cell = [[Storage()
                          for i in xrange(numcols)]
                         for j in xrange(numrows)]
        cells = self.cell

        all_values = []
        for r in xrange(numrows):

            # Initialize row header
            row = rows[r]
            row[RECORDS] = []
            row[VALUES] = []

            row_records = row[RECORDS]
            row_values = row[VALUES]

            for c in xrange(numcols):

                # Initialize column header
                col = cols[c]
                if RECORDS not in col:
                    col[RECORDS] = []
                col_records = col[RECORDS]
                if VALUES not in col:
                    col[VALUES] = []
                col_values = col[VALUES]

                # Get the records
                cell = cells[r][c]
                if RECORDS in cell and cell[RECORDS] is not None:
                    ids = cell[RECORDS]
                else:
                    data = pt[r][c]
                    if data:
                        remove = data.remove
                        while None in data:
                            remove(None)
                        ids = data
                    else:
                        ids = []
                    cell[RECORDS] = ids
                row_records.extend(ids)
                col_records.extend(ids)

                # Get the values
                if fact is None:
                    fact = pkey
                    values = ids
                    row_values = row_records
                    col_values = row_records
                    all_values = self.records.keys()
                else:
                    values = []
                    append = values.append
                    for i in ids:
                        value = extract(records[i], fact)
                        if value is None:
                            continue
                        append(value)
                    if len(values) and type(values[0]) is list:
                        values = reduce(lambda x, y: x.extend(y) or x, values)
                    if method in ("list", "count"):
                        values =  list(set(values))
                    row_values.extend(values)
                    col_values.extend(values)
                    all_values.extend(values)

                # Aggregate values
                value = aggregate(values, method)
                cell[layer] = value

            # Compute row total
            row[layer] = aggregate(row_values, method)
            del row[VALUES]

        # Compute column total
        for c in xrange(numcols):
            col = cols[c]
            col[layer] = aggregate(col[VALUES], method)
            del col[VALUES]

        # Compute overall total
        self.totals[layer] = aggregate(all_values, method)
        return

    # -------------------------------------------------------------------------
    @staticmethod
    def _aggregate(values, method):
        """
            Compute an aggregation of atomic values

            @param values: the values
            @param method: the aggregation method
        """

        if values is None:
            return None

        if method is None or method == "list":
            if values:
                return values
            else:
                return None

        elif method == "count":
            return len(values)

        elif method == "min":
            try:
                return min(values)
            except TypeError:
                return None

        elif method == "max":
            try:
                return max(values)
            except TypeError, ValueError:
                return None

        elif method == "sum":
            try:
                return sum(values)
            except TypeError, ValueError:
                return None

        elif method in ("avg"):
            try:
                if len(values):
                    return sum(values) / float(len(values))
                else:
                    return 0.0
            except TypeError, ValueError:
                return None

        #elif method == "std":
            #import numpy
            #if not values:
                #return 0.0
            #try:
                #return numpy.std(values)
            #except TypeError, ValueError:
                #return None

        else:
            return None

    # -------------------------------------------------------------------------
    def __len__(self):
        """ Total number of records in the report """

        items = self.records
        if items is None:
            return 0
        else:
            return len(self.records)

# =============================================================================

class S3ContingencyTable(TABLE):
    """ HTML Helper to generate a contingency table """

    def __init__(self, report, show_totals=True, **attributes):
        """
            Constructor

            @param report: the S3Report
            @param attributes: the HTML attributes for the table
        """

        T = current.T
        TOTAL = T("Total")

        TABLE.__init__(self, **attributes)
        components = self.components = []
        self.json_data = None

        layers = report.layers
        resource = report.resource
        tablename = resource.tablename

        cols = report.cols
        rows = report.rows
        numcols = report.numcols
        numrows = report.numrows
        lfields = report.lfields

        get_label = self._get_label
        get_total = self._totals
        represent = lambda f, v, d="": \
                    self._represent(lfields, f, v, default=d)

        layer_label = None
        col_titles = []
        add_col_title = col_titles.append
        col_totals = []
        add_col_total = col_totals.append
        row_titles = []
        add_row_title = row_titles.append
        row_totals = []
        add_row_total = row_totals.append

        # Table header --------------------------------------------------------
        #

        # Layer titles
        labels = []
        get_mname = S3Cube.mname
        for field_name, method in layers:
            label = get_label(lfields, field_name, tablename, "fact")
            mname = get_mname(method)
            if not labels:
                m = method == "list" and get_mname("count") or mname
                layer_label = "%s (%s)" % (label, m)
            labels.append("%s (%s)" % (label, mname))
        layers_title = TH(" / ".join(labels))

        # Columns field title
        if cols:
            col_label = get_label(lfields, cols, tablename, "cols")
            _colspan = numcols + 1
        else:
            col_label = ""
            _colspan = numcols
        cols_title = TH(col_label, _colspan=_colspan, _scope="col")

        titles = TR(layers_title, cols_title)

        # Rows field title
        row_label = get_label(lfields, rows, tablename, "rows")
        rows_title = TH(row_label, _scope="col")

        headers = TR(rows_title)
        add_header = headers.append

        # Column headers
        values = report.col
        for i in xrange(numcols):
            value = values[i].value
            v = represent(cols, value)
            add_col_title(s3_truncate(unicode(v)))
            colhdr = TH(v, _scope="col")
            add_header(colhdr)

        # Row totals header
        if show_totals and cols is not None:
            add_header(TH(TOTAL, _class="totals_header rtotal", _scope="col"))

        thead = THEAD(titles, headers)

        # Table body ----------------------------------------------------------
        #

        tbody = TBODY()
        add_row = tbody.append

        # lookup table for cell list values
        cell_lookup_table = {} # {{}, {}}

        cells = report.cell
        rvals = report.row

        for i in xrange(numrows):

            # Initialize row
            _class = i % 2 and "odd" or "even"
            tr = TR(_class=_class)
            add_cell = tr.append

            # Row header
            row = rvals[i]
            v = represent(rows, row.value)
            add_row_title(s3_truncate(unicode(v)))
            rowhdr = TD(v)
            add_cell(rowhdr)

            # Result cells
            for j in xrange(numcols):
                cell = cells[i][j]
                vals = []
                cell_ids = []
                add_value = vals.append
                for layer_idx, layer in enumerate(layers):
                    f, m = layer
                    value = cell[layer]
                    if m == "list":
                        if isinstance(value, list):
                            l = [represent(f, v, d="-") for v in value]
                        elif value is None:
                            l = "-"
                        else:
                            if type(value) in (int, float):
                                l = IS_NUMBER.represent(value)
                            else:
                                l = unicode(value)
                        add_value(", ".join(l))
                    else:
                        if type(value) in (int, float):
                            add_value(IS_NUMBER.represent(value))
                        else:
                            add_value(unicode(value))

                    # hold the references
                    layer_ids = []
                    # get previous lookup values for this layer
                    layer_values = cell_lookup_table.get(layer_idx, {})

                    if m == "count":
                        for id in cell.records:
                            # cell.records == [#, #, #]
                            field = lfields[f].field
                            record = report.records[id]

                            if field.tablename in record:
                                fvalue = record[field.tablename][field.name]
                            else:
                                fvalue = record[field.name]

                            if fvalue is not None:
                                if s3_has_foreign_key(field):
                                    if not isinstance(fvalue, list):
                                        fvalue = [fvalue]

                                    # list of foreign keys
                                    for fk in fvalue:
                                        if fk not in layer_ids:
                                            layer_ids.append(fk)
                                            layer_values[fk] = str(field.represent(fk))
                                else:
                                    if id not in layer_ids:
                                        layer_ids.append(id)
                                        layer_values[id] = s3_unicode(represent(f, fvalue))


                    cell_ids.append(layer_ids)
                    cell_lookup_table[layer_idx] = layer_values

                vals = " / ".join(vals)

                if any(cell_ids):
                    cell_attr = {
                        "_data-records": cell_ids
                    }
                    vals = (A(_class="report-cell-zoom"), vals)
                else:
                    cell_attr = {}

                add_cell(TD(vals, **cell_attr))

            # Row total
            totals = get_total(row, layers, append=add_row_total)
            if show_totals and cols is not None:
                add_cell(TD(totals))

            add_row(tr)

        # Table footer --------------------------------------------------------
        #
        i = numrows
        _class = i % 2 and "odd" or "even"
        _class = "%s %s" % (_class, "totals_row")

        col_total = TR(_class=_class)
        add_total = col_total.append
        add_total(TH(TOTAL, _class="totals_header", _scope="row"))

        # Column totals
        for j in xrange(numcols):
            col = report.col[j]
            totals = get_total(col, layers, append=add_col_total)
            add_total(TD(IS_NUMBER.represent(totals)))

        # Grand total
        if cols is not None:
            grand_totals = get_total(report.totals, layers)
            add_total(TD(grand_totals))

        tfoot = TFOOT(col_total)

        # Wrap up -------------------------------------------------------------
        #
        append = components.append
        append(thead)
        append(tbody)
        if show_totals:
            append(tfoot)

        # Chart data ----------------------------------------------------------
        #
        drows = dcols = None
        BY = T("by")
        top = self._top
        if rows and row_titles and row_totals:
            drows = top(zip(row_titles, row_totals))
        if cols and col_titles and col_totals:
            dcols = top(zip(col_titles, col_totals))
        row_label = "%s %s" % (BY, str(row_label))
        if col_label:
            col_label = "%s %s" % (BY, str(col_label))
        layer_label=str(layer_label)

        json_data = json.dumps(dict(rows=drows,
                                    cols=dcols,
                                    row_label=row_label,
                                    col_label=col_label,
                                    layer_label=layer_label,
                                    cell_lookup_table=cell_lookup_table
                                   ))
        self.report_data = Storage(row_label=row_label,
                                   col_label=col_label,
                                   layer_label=layer_label,
                                   json_data=json_data)

    # -------------------------------------------------------------------------
    @staticmethod
    def _top(tl, length=10, least=False):
        """
            From a list of tuples (n, v) containing more than N elements,
            selects the top (or least) N (by v) and sums up the others as
            new element "Others".

            @param tl: the tuple list
            @param length: the maximum length N of the result list
            @param reverse: select the least N instead
        """
        try:
            if len(tl) > length:
                m = length - 1
                l = list(tl)
                l.sort(lambda x, y: int(y[1]-x[1]))
                if least:
                    l.reverse()
                ts = (str(current.T("Others")),
                      reduce(lambda s, t: s+t[1], l[m:], 0))
                l = l[:m] + [ts]
                return l
        except (TypeError, ValueError):
            pass
        return tl

    # -------------------------------------------------------------------------
    @staticmethod
    def _totals(values, layers, append=None):
        """
            Get the totals of a row/column/report

            @param values: the values dictionary
            @param layers: the layers
            @param append: callback to collect the totals for JSON data
                           (currently only collects the first layer)
        """

        totals = []
        for layer in layers:
            f, m = layer
            value = values[layer]

            if m == "list":
                value = value and len(value) or 0
            if not len(totals) and append is not None:
                append(value)
            totals.append(IS_NUMBER.represent(value))
        totals = " / ".join(totals)
        return totals

    # -------------------------------------------------------------------------
    @staticmethod
    def _represent(lfields, field, value, default="-"):
        """
            Represent a field value

            @param lfields: the list fields map
            @param field: the field
            @param value: the value
            @param default: the default representation
        """

        if field in lfields:
            lfield = lfields[field]
            if lfield.field:
                f = lfield.field
                ftype = str(f.type)
                if ftype not in ("string", "text") and \
                   isinstance(value, basestring):
                    # pyvttbl converts col/row headers into unicode,
                    # but represent may need the original data type,
                    # hence try to convert it back here:
                    convert = S3TypeConverter.convert
                    try:
                        if ftype == "boolean":
                            value = convert(bool, value)
                        elif ftype == "integer":
                            value = convert(int, value)
                        elif ftype == "float":
                            value = convert(float, value)
                        elif ftype == "date":
                            value = convert(datetime.date, value)
                        elif ftype == "time":
                            value = convert(datetime.time, value)
                        elif ftype == "datetime":
                            value = convert(datetime.datetime, value)
                    except TypeError, ValueError:
                        pass
                return current.manager.represent(lfield.field, value,
                                                 strip_markup=True)
        if value is None:
            return default
        else:
            return unicode(value)

    # -------------------------------------------------------------------------
    @staticmethod
    def _get_label(lfields, field, tablename, key):
        """
            Get the label for a field

            @param lfields: the list fields map
            @param key: the configuration key
        """

        DEFAULT = ""

        if field in lfields:
            lf = lfields[field]
        else:
            return DEFAULT
        get_config = lambda key, default, tablename=tablename: \
                     current.s3db.get_config(tablename, key, default)
        list_fields = get_config("list_fields", None)
        fields = get_config(key, list_fields)
        if fields:
            for f in fields:
                if isinstance(f, (tuple, list)) and f[1] == lf.selector:
                    return f[0]
        if lf:
            return lf.label
        else:
            return DEFAULT

# END =========================================================================
