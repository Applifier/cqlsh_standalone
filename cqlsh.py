#!/usr/bin/env python

# Licensed to the Apache Software Foundation (ASF) under one
# or more contributor license agreements.  See the NOTICE file
# distributed with this work for additional information
# regarding copyright ownership.  The ASF licenses this file
# to you under the Apache License, Version 2.0 (the
# "License"); you may not use this file except in compliance
# with the License.  You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.


from __future__ import with_statement

description = "CQL Shell for Apache Cassandra"
version = "4.1.1"

from StringIO import StringIO
from itertools import groupby
from contextlib import contextmanager, closing
from glob import glob
from uuid import UUID

import cmd
import sys
import os
import time
import optparse
import ConfigParser
import codecs
import locale
import platform
import warnings
import csv
import getpass


readline = None
try:
    # check if tty first, cause readline doesn't check, and only cares
    # about $TERM. we don't want the funky escape code stuff to be
    # output if not a tty.
    if sys.stdin.isatty():
        import readline
except ImportError:
    pass

CQL_LIB_PREFIX = 'cql-internal-only-'
THRIFT_LIB_PREFIX = 'thrift-python-internal-only-'

CASSANDRA_PATH = os.path.dirname(os.path.realpath(__file__))


# use bundled libs for python-cql and thrift, if available. if there
# is a ../lib dir, use bundled libs there preferentially.
ZIPLIB_DIRS = [os.path.join(CASSANDRA_PATH, 'lib')]

def find_zip(libprefix):
    for ziplibdir in ZIPLIB_DIRS:
        zips = glob(os.path.join(ziplibdir, libprefix + '*.zip'))
        if zips:
            return max(zips)   # probably the highest version, if multiple

cql_zip = find_zip(CQL_LIB_PREFIX)
if cql_zip:
    ver = os.path.splitext(os.path.basename(cql_zip))[0][len(CQL_LIB_PREFIX):]
    sys.path.insert(0, os.path.join(cql_zip, 'cql-' + ver))
thrift_zip = find_zip(THRIFT_LIB_PREFIX)
if thrift_zip:
    sys.path.insert(0, thrift_zip)

try:
    import cql
except ImportError, e:
    sys.exit("\nPython CQL driver not installed, or not on PYTHONPATH.\n"
             'You might try "easy_install cql".\n\n'
             'Python: %s\n'
             'Module load path: %r\n\n'
             'Error: %s\n' % (sys.executable, sys.path, e))

import cql.decoders
from cql.cursor import _VOID_DESCRIPTION
from cql.cqltypes import (cql_types, cql_typename, lookup_casstype, lookup_cqltype,
                          CassandraType, ReversedType, CompositeType)

# cqlsh should run correctly when run out of a Cassandra source tree,
# out of an unpacked Cassandra tarball, and after a proper package install.
cqlshlibdir = os.path.join(CASSANDRA_PATH, 'lib')
if os.path.isdir(cqlshlibdir):
    sys.path.insert(0, cqlshlibdir)

from cqlshlib import cqlhandling, cql3handling, pylexotron
from cqlshlib.displaying import (RED, BLUE, ANSI_RESET, COLUMN_NAME_COLORS,
                                 FormattedValue, colorme)
from cqlshlib.formatting import format_by_type
from cqlshlib.util import trim_if_present
from cqlshlib.tracing import print_trace_session

HISTORY_DIR = os.path.expanduser(os.path.join('~', '.cassandra'))
CONFIG_FILE = os.path.join(HISTORY_DIR, 'cqlshrc')
HISTORY = os.path.join(HISTORY_DIR, 'cqlsh_history')
if not os.path.exists(HISTORY_DIR):
    try:
        os.mkdir(HISTORY_DIR)
    except OSError:
        print '\nWarning: Cannot create directory at `%s`. Command history will not be saved.\n' % HISTORY_DIR

OLD_CONFIG_FILE = os.path.expanduser(os.path.join('~', '.cqlshrc'))
if os.path.exists(OLD_CONFIG_FILE):
    os.rename(OLD_CONFIG_FILE, CONFIG_FILE)
OLD_HISTORY = os.path.expanduser(os.path.join('~', '.cqlsh_history'))
if os.path.exists(OLD_HISTORY):
    os.rename(OLD_HISTORY, HISTORY)

DEFAULT_HOST = 'cassandra-a-1'
DEFAULT_PORT = 9160
DEFAULT_CQLVER = '3.1.1'
DEFAULT_TRANSPORT_FACTORY = 'cqlshlib.tfactory.regular_transport_factory'

DEFAULT_TIME_FORMAT = '%Y-%m-%d %H:%M:%S%z'
DEFAULT_FLOAT_PRECISION = 5
DEFAULT_SELECT_LIMIT = 10000

if readline is not None and readline.__doc__ is not None and 'libedit' in readline.__doc__:
    DEFAULT_COMPLETEKEY = '\t'
else:
    DEFAULT_COMPLETEKEY = 'tab'

epilog = """Connects to %(DEFAULT_HOST)s:%(DEFAULT_PORT)d by default. These
defaults can be changed by setting $CQLSH_HOST and/or $CQLSH_PORT. When a
host (and optional port number) are given on the command line, they take
precedence over any defaults.""" % globals()

parser = optparse.OptionParser(description=description, epilog=epilog,
                               usage="Usage: %prog [options] [host [port]]",
                               version='cqlsh ' + version)
parser.add_option("-C", "--color", action='store_true', dest='color',
                  help='Always use color output')
parser.add_option("--no-color", action='store_false', dest='color',
                  help='Never use color output')
parser.add_option("-u", "--username", help="Authenticate as user.")
parser.add_option("-p", "--password", help="Authenticate using password.")
parser.add_option('-k', '--keyspace', help='Authenticate to the given keyspace.')
parser.add_option("-f", "--file", help="Execute commands from FILE, then exit")
parser.add_option("-t", "--transport-factory",
                  help="Use the provided Thrift transport factory function.")
parser.add_option('--debug', action='store_true',
                  help='Show additional debugging information')
parser.add_option('--cqlversion', default=DEFAULT_CQLVER,
                  help='Specify a particular CQL version (default: %default).'
                       ' Examples: "3.0.3", "3.1.0"')
parser.add_option("-e", "--execute", help='Execute the statement and quit.')

CQL_ERRORS = (cql.Error,)
try:
    from thrift.Thrift import TException
except ImportError:
    pass
else:
    CQL_ERRORS += (TException,)

debug_completion = bool(os.environ.get('CQLSH_DEBUG_COMPLETION', '') == 'YES')

SYSTEM_KEYSPACES = ('system', 'system_traces', 'system_auth')

# we want the cql parser to understand our cqlsh-specific commands too
my_commands_ending_with_newline = (
    'help',
    '?',
    'consistency',
    'describe',
    'desc',
    'show',
    'source',
    'capture',
    'debug',
    'tracing',
    'expand',
    'exit',
    'quit'
)

cqlsh_syntax_completers = []
def cqlsh_syntax_completer(rulename, termname):
    def registrator(f):
        cqlsh_syntax_completers.append((rulename, termname, f))
        return f
    return registrator

cqlsh_extra_syntax_rules = r'''
<cqlshCommand> ::= <CQL_Statement>
                 | <specialCommand> ( ";" | "\n" )
                 ;

<specialCommand> ::= <describeCommand>
                   | <consistencyCommand>
                   | <showCommand>
                   | <sourceCommand>
                   | <captureCommand>
                   | <copyCommand>
                   | <debugCommand>
                   | <helpCommand>
                   | <tracingCommand>
                   | <expandCommand>
                   | <exitCommand>
                   ;

<describeCommand> ::= ( "DESCRIBE" | "DESC" )
                                  ( "KEYSPACES"
                                  | "KEYSPACE" ksname=<keyspaceName>?
                                  | ( "COLUMNFAMILY" | "TABLE" ) cf=<columnFamilyName>
                                  | ( "COLUMNFAMILIES" | "TABLES" )
                                  | "FULL"? "SCHEMA"
                                  | "CLUSTER" )
                    ;

<consistencyCommand> ::= "CONSISTENCY" ( level=<consistencyLevel> )?
                       ;

<consistencyLevel> ::= "ANY"
                     | "ONE"
                     | "TWO"
                     | "THREE"
                     | "QUORUM"
                     | "ALL"
                     | "LOCAL_ONE"
                     | "LOCAL_QUORUM"
                     | "EACH_QUORUM"
                     ;

<showCommand> ::= "SHOW" what=( "VERSION" | "HOST" | "SESSION" sessionid=<uuid> )
                ;

<sourceCommand> ::= "SOURCE" fname=<stringLiteral>
                  ;

<captureCommand> ::= "CAPTURE" ( fname=( <stringLiteral> | "OFF" ) )?
                   ;

<copyCommand> ::= "COPY" cf=<columnFamilyName>
                         ( "(" [colnames]=<colname> ( "," [colnames]=<colname> )* ")" )?
                         ( dir="FROM" ( fname=<stringLiteral> | "STDIN" )
                         | dir="TO"   ( fname=<stringLiteral> | "STDOUT" ) )
                         ( "WITH" <copyOption> ( "AND" <copyOption> )* )?
                ;

<copyOption> ::= [optnames]=<identifier> "=" [optvals]=<copyOptionVal>
               ;

<copyOptionVal> ::= <identifier>
                  | <stringLiteral>
                  ;

# avoiding just "DEBUG" so that this rule doesn't get treated as a terminal
<debugCommand> ::= "DEBUG" "THINGS"?
                 ;

<helpCommand> ::= ( "HELP" | "?" ) [topic]=( /[a-z_]*/ )*
                ;

<tracingCommand> ::= "TRACING" ( switch=( "ON" | "OFF" ) )?
                   ;

<expandCommand> ::= "EXPAND" ( switch=( "ON" | "OFF" ) )?
                   ;

<exitCommand> ::= "exit" | "quit"
                ;

<qmark> ::= "?" ;
'''

@cqlsh_syntax_completer('helpCommand', 'topic')
def complete_help(ctxt, cqlsh):
    return sorted([ t.upper() for t in cqldocs.get_help_topics() + cqlsh.get_help_topics() ])

def complete_source_quoted_filename(ctxt, cqlsh):
    partial = ctxt.get_binding('partial', '')
    head, tail = os.path.split(partial)
    exhead = os.path.expanduser(head)
    try:
        contents = os.listdir(exhead or '.')
    except OSError:
        return ()
    matches = filter(lambda f: f.startswith(tail), contents)
    annotated = []
    for f in matches:
        match = os.path.join(head, f)
        if os.path.isdir(os.path.join(exhead, f)):
            match += '/'
        annotated.append(match)
    return annotated

cqlsh_syntax_completer('sourceCommand', 'fname') \
        (complete_source_quoted_filename)
cqlsh_syntax_completer('captureCommand', 'fname') \
        (complete_source_quoted_filename)

@cqlsh_syntax_completer('copyCommand', 'fname')
def copy_fname_completer(ctxt, cqlsh):
    lasttype = ctxt.get_binding('*LASTTYPE*')
    if lasttype == 'unclosedString':
        return complete_source_quoted_filename(ctxt, cqlsh)
    partial = ctxt.get_binding('partial')
    if partial == '':
        return ["'"]
    return ()

@cqlsh_syntax_completer('copyCommand', 'colnames')
def complete_copy_column_names(ctxt, cqlsh):
    existcols = map(cqlsh.cql_unprotect_name, ctxt.get_binding('colnames', ()))
    ks = cqlsh.cql_unprotect_name(ctxt.get_binding('ksname', None))
    cf = cqlsh.cql_unprotect_name(ctxt.get_binding('cfname'))
    colnames = cqlsh.get_column_names(ks, cf)
    if len(existcols) == 0:
        return [colnames[0]]
    return set(colnames[1:]) - set(existcols)

COPY_OPTIONS = ('DELIMITER', 'QUOTE', 'ESCAPE', 'HEADER', 'ENCODING', 'NULL')

@cqlsh_syntax_completer('copyOption', 'optnames')
def complete_copy_options(ctxt, cqlsh):
    optnames = map(str.upper, ctxt.get_binding('optnames', ()))
    direction = ctxt.get_binding('dir').upper()
    opts = set(COPY_OPTIONS) - set(optnames)
    if direction == 'FROM':
        opts -= ('ENCODING',)
    return opts

@cqlsh_syntax_completer('copyOption', 'optvals')
def complete_copy_opt_values(ctxt, cqlsh):
    optnames = ctxt.get_binding('optnames', ())
    lastopt = optnames[-1].lower()
    if lastopt == 'header':
        return ['true', 'false']
    return [cqlhandling.Hint('<single_character_string>')]

class NoKeyspaceError(Exception):
    pass

class KeyspaceNotFound(Exception):
    pass

class ColumnFamilyNotFound(Exception):
    pass

class VersionNotSupported(Exception):
    pass

class DecodeError(Exception):
    verb = 'decode'

    def __init__(self, thebytes, err, expectedtype, colname=None):
        self.thebytes = thebytes
        self.err = err
        if isinstance(expectedtype, type) and issubclass(expectedtype, CassandraType):
            expectedtype = expectedtype.cql_parameterized_type()
        self.expectedtype = expectedtype
        self.colname = colname

    def __str__(self):
        return str(self.thebytes)

    def message(self):
        what = 'value %r' % (self.thebytes,)
        if self.colname is not None:
            what = 'value %r (for column %r)' % (self.thebytes, self.colname)
        return 'Failed to %s %s as %s: %s' \
               % (self.verb, what, self.expectedtype, self.err)

    def __repr__(self):
        return '<%s %s>' % (self.__class__.__name__, self.message())

class FormatError(DecodeError):
    verb = 'format'

def full_cql_version(ver):
    while ver.count('.') < 2:
        ver += '.0'
    ver_parts = ver.split('-', 1) + ['']
    vertuple = tuple(map(int, ver_parts[0].split('.')) + [ver_parts[1]])
    return ver, vertuple

def format_value(val, typeclass, output_encoding, addcolor=False, time_format=None,
                 float_precision=None, colormap=None, nullval=None):
    if isinstance(val, DecodeError):
        if addcolor:
            return colorme(repr(val.thebytes), colormap, 'error')
        else:
            return FormattedValue(repr(val.thebytes))
    if not issubclass(typeclass, CassandraType):
        typeclass = lookup_casstype(typeclass)
    return format_by_type(typeclass, val, output_encoding, colormap=colormap,
                          addcolor=addcolor, nullval=nullval, time_format=time_format,
                          float_precision=float_precision)

def show_warning_without_quoting_line(message, category, filename, lineno, file=None, line=None):
    if file is None:
        file = sys.stderr
    try:
        file.write(warnings.formatwarning(message, category, filename, lineno, line=''))
    except IOError:
        pass
warnings.showwarning = show_warning_without_quoting_line
warnings.filterwarnings('always', category=cql3handling.UnexpectedTableStructure)

def describe_interval(seconds):
    desc = []
    for length, unit in ((86400, 'day'), (3600, 'hour'), (60, 'minute')):
        num = int(seconds) / length
        if num > 0:
            desc.append('%d %s' % (num, unit))
            if num > 1:
                desc[-1] += 's'
        seconds %= length
    words = '%.03f seconds' % seconds
    if len(desc) > 1:
        words = ', '.join(desc) + ', and ' + words
    elif len(desc) == 1:
        words = desc[0] + ' and ' + words
    return words

class Shell(cmd.Cmd):
    custom_prompt = os.getenv('CQLSH_PROMPT', '')
    if custom_prompt is not '':
        custom_prompt += "\n"
    default_prompt = custom_prompt + "cqlsh> "
    continue_prompt = "   ... "
    keyspace_prompt = custom_prompt + "cqlsh:%s> "
    keyspace_continue_prompt = "%s    ... "
    num_retries = 4
    show_line_nums = False
    debug = False
    stop = False
    last_hist = None
    shunted_query_out = None
    csv_dialect_defaults = dict(delimiter=',', doublequote=False,
                                escapechar='\\', quotechar='"')

    def __init__(self, hostname, port, transport_factory, color=False,
                 username=None, password=None, encoding=None, stdin=None, tty=True,
                 completekey=DEFAULT_COMPLETEKEY, use_conn=None,
                 cqlver=DEFAULT_CQLVER, keyspace=None,
                 tracing_enabled=False, expand_enabled=False,
                 display_time_format=DEFAULT_TIME_FORMAT,
                 display_float_precision=DEFAULT_FLOAT_PRECISION,
                 single_statement=None):
        cmd.Cmd.__init__(self, completekey=completekey)
        self.hostname = hostname
        self.port = port
        self.transport_factory = transport_factory

        if username and not password:
            password = getpass.getpass()

        self.username = username
        self.password = password
        self.keyspace = keyspace
        self.tracing_enabled = tracing_enabled
        self.expand_enabled = expand_enabled
        if use_conn is not None:
            self.conn = use_conn
        else:
            transport = transport_factory(hostname, port, os.environ, CONFIG_FILE)
            self.conn = cql.connect(hostname, port, user=username, password=password,
                                    cql_version=cqlver, transport=transport)
        self.set_expanded_cql_version(cqlver)
        # we could set the keyspace through cql.connect(), but as of 1.0.10,
        # it doesn't quote the keyspace for USE :(
        if keyspace is not None:
            tempcurs = self.conn.cursor()
            tempcurs.execute('USE %s;' % self.cql_protect_name(keyspace))
            tempcurs.close()
        self.cursor = self.conn.cursor()
        self.get_connection_versions()

        self.current_keyspace = keyspace

        self.color = color
        self.display_time_format = display_time_format
        self.display_float_precision = display_float_precision
        if encoding is None:
            encoding = locale.getpreferredencoding()
        self.encoding = encoding
        self.output_codec = codecs.lookup(encoding)

        self.statement = StringIO()
        self.lineno = 1
        self.in_comment = False

        self.prompt = ''
        if stdin is None:
            stdin = sys.stdin
        self.tty = tty
        if tty:
            self.reset_prompt()
            self.report_connection()
            print 'Use HELP for help.'
        else:
            self.show_line_nums = True
        self.stdin = stdin
        self.query_out = sys.stdout
        self.empty_lines = 0
        self.statement_error = False
        self.single_statement = single_statement
        # see CASSANDRA-7399
        cql.cqltypes.CompositeType.cql_parameterized_type = classmethod(lambda cls: "'%s'" % cls.cass_parameterized_type_with(cls.subtypes, True))

    def set_expanded_cql_version(self, ver):
        ver, vertuple = full_cql_version(ver)
        self.set_cql_version(ver)
        self.cql_version = ver
        self.cql_ver_tuple = vertuple

    def cqlver_atleast(self, major, minor=0, patch=0):
        return self.cql_ver_tuple[:3] >= (major, minor, patch)

    def cassandraver_atleast(self, major, minor=0, patch=0):
        return self.cass_ver_tuple[:3] >= (major, minor, patch)

    def myformat_value(self, val, casstype, **kwargs):
        if isinstance(val, DecodeError):
            self.decoding_errors.append(val)
        try:
            return format_value(val, casstype, self.output_codec.name,
                                addcolor=self.color, time_format=self.display_time_format,
                                float_precision=self.display_float_precision, **kwargs)
        except Exception, e:
            err = FormatError(val, e, casstype)
            self.decoding_errors.append(err)
            return format_value(err, None, self.output_codec.name, addcolor=self.color)

    def myformat_colname(self, name, nametype):
        return self.myformat_value(name, nametype, colormap=COLUMN_NAME_COLORS)

    # cql/cursor.py:Cursor.decode_row() function, modified to not turn '' into None.
    def decode_row(self, cursor, row):
        values = []
        bytevals = cursor.columnvalues(row)
        for val, vtype, nameinfo in zip(bytevals, cursor.column_types, cursor.name_info):
            if val == '':
                values.append(val)
            else:
                values.append(cursor.decoder.decode_value(val, vtype, nameinfo[0]))
        return values

    def report_connection(self):
        self.show_host()
        self.show_version()

    def show_host(self):
        print "Connected to %s at %s:%d." % \
               (self.applycolor(self.get_cluster_name(), BLUE),
                self.hostname,
                self.port)

    def show_version(self):
        vers = self.connection_versions.copy()
        vers['shver'] = version
        # system.Versions['cql'] apparently does not reflect changes with
        # set_cql_version.
        vers['cql'] = self.cql_version
        print "[cqlsh %(shver)s | Cassandra %(build)s | CQL spec %(cql)s | Thrift protocol %(thrift)s]" % vers

    def show_session(self, sessionid):
        print_trace_session(self, self.cursor, sessionid)

    def get_connection_versions(self):
        self.cursor.execute("select * from system.local where key = 'local'")
        result = self.fetchdict()
        vers = {
            'build': result['release_version'],
            'thrift': result['thrift_version'],
            'cql': result['cql_version'],
        }
        self.connection_versions = vers
        self.cass_ver_tuple = tuple(map(int, vers['build'].split('-', 1)[0].split('.')[:3]))

    def fetchdict(self):
        row = self.cursor.fetchone()
        if row is None:
            return None
        desc = self.cursor.description
        return dict(zip([d[0] for d in desc], row))

    def fetchdict_all(self):
        dicts = []
        for row in self.cursor:
            desc = self.cursor.description
            dicts.append(dict(zip([d[0] for d in desc], row)))
        return dicts

    def get_keyspace_names(self):
        return [k.name for k in self.get_keyspaces()]

    def get_columnfamily_names(self, ksname=None):
        if ksname is None:
            ksname = self.current_keyspace
        cf_q = """select columnfamily_name from system.schema_columnfamilies
                   where keyspace_name=:ks"""
        self.cursor.execute(cf_q,
                            {'ks': self.cql_unprotect_name(ksname)},
                            consistency_level='ONE')
        return [str(row[0]) for row in self.cursor.fetchall()]

    def get_index_names(self, ksname=None):
        idxnames = []
        for cfname in self.get_columnfamily_names(ksname=ksname):
            for col in self.get_columnfamily_layout(ksname, cfname).columns:
                if col.index_name is not None:
                    idxnames.append(col.index_name)
        return idxnames

    def get_column_names(self, ksname, cfname):
        if ksname is None:
            ksname = self.current_keyspace
        layout = self.get_columnfamily_layout(ksname, cfname)
        return [col.name for col in layout.columns]

    # ===== thrift-dependent parts =====

    def get_cluster_name(self):
        return self.make_hacktastic_thrift_call('describe_cluster_name')

    def get_partitioner(self):
        return self.make_hacktastic_thrift_call('describe_partitioner')

    def get_snitch(self):
        return self.make_hacktastic_thrift_call('describe_snitch')

    def get_thrift_version(self):
        return self.make_hacktastic_thrift_call('describe_version')

    def get_ring(self):
        if self.current_keyspace is None or self.current_keyspace == 'system':
            raise NoKeyspaceError("Ring view requires a current non-system keyspace")
        return self.make_hacktastic_thrift_call('describe_ring', self.current_keyspace)

    def get_keyspace(self, ksname):
        try:
            return self.make_hacktastic_thrift_call('describe_keyspace', ksname)
        except cql.cassandra.ttypes.NotFoundException:
            raise KeyspaceNotFound('Keyspace %r not found.' % ksname)

    def get_keyspaces(self):
        return self.make_hacktastic_thrift_call('describe_keyspaces')

    def get_schema_versions(self):
        return self.make_hacktastic_thrift_call('describe_schema_versions')

    def set_cql_version(self, ver):
        try:
            return self.make_hacktastic_thrift_call('set_cql_version', ver)
        except cql.cassandra.ttypes.InvalidRequestException, e:
            raise VersionNotSupported(e.why)

    def trace_next_query(self):
        return self.make_hacktastic_thrift_call('trace_next_query')

    def make_hacktastic_thrift_call(self, call, *args):
        client = self.conn.client
        return getattr(client, call)(*args)

    # ===== end thrift-dependent parts =====

    # ===== cql3-dependent parts =====

    def get_columnfamily_layout(self, ksname, cfname):
        if ksname is None:
            ksname = self.current_keyspace
        cf_q = """select * from system.schema_columnfamilies
                   where keyspace_name=:ks and columnfamily_name=:cf"""
        col_q = """select * from system.schema_columns
                    where keyspace_name=:ks and columnfamily_name=:cf"""
        self.cursor.execute(cf_q,
                            {'ks': ksname, 'cf': cfname},
                            consistency_level='ONE')
        layout = self.fetchdict()
        if layout is None:
            raise ColumnFamilyNotFound("Column family %r not found" % cfname)
        self.cursor.execute(col_q,
                            {'ks': ksname, 'cf': cfname},
                            consistency_level='ONE')
        cols = self.fetchdict_all()
        return cql3handling.CqlTableDef.from_layout(layout, cols)

    # ===== end cql3-dependent parts =====

    def reset_statement(self):
        self.reset_prompt()
        self.statement.truncate(0)
        self.empty_lines = 0;

    def reset_prompt(self):
        if self.current_keyspace is None:
            self.set_prompt(self.default_prompt)
        else:
            self.set_prompt(self.keyspace_prompt % self.current_keyspace)

    def set_continue_prompt(self):
        if self.empty_lines >=3:
            self.set_prompt("Statements are terminated with a ';'.  You can press CTRL-C to cancel an incomplete statement.")
            self.empty_lines = 0
            return
        if self.current_keyspace is None:
            self.set_prompt(self.continue_prompt)
        else:
            spaces = ' ' * len(str(self.current_keyspace))
            self.set_prompt(self.keyspace_continue_prompt % spaces)
        self.empty_lines = self.empty_lines + 1 if not self.lastcmd else 0

    @contextmanager
    def prepare_loop(self):
        readline = None
        if self.tty and self.completekey:
            try:
                import readline
            except ImportError:
                pass
            else:
                old_completer = readline.get_completer()
                readline.set_completer(self.complete)
                if readline.__doc__ is not None and 'libedit' in readline.__doc__:
                    readline.parse_and_bind("bind -e")
                    readline.parse_and_bind("bind '" + self.completekey + "' rl_complete")
                else:
                    readline.parse_and_bind(self.completekey + ": complete")
        try:
            yield
        finally:
            if readline is not None:
                readline.set_completer(old_completer)

    def get_input_line(self, prompt=''):
        if self.tty:
            self.lastcmd = raw_input(prompt)
            line = self.lastcmd + '\n'
        else:
            self.lastcmd = self.stdin.readline()
            line = self.lastcmd
            if not len(line):
                raise EOFError
        self.lineno += 1
        return line

    def use_stdin_reader(self, until='', prompt=''):
        until += '\n'
        while True:
            try:
                newline = self.get_input_line(prompt=prompt)
            except EOFError:
                return
            if newline == until:
                return
            yield newline

    def cmdloop(self):
        """
        Adapted from cmd.Cmd's version, because there is literally no way with
        cmd.Cmd.cmdloop() to tell the difference between "EOF" showing up in
        input and an actual EOF.
        """
        with self.prepare_loop():
            while not self.stop:
                try:
                    if self.single_statement:
                        line = self.single_statement
                        self.stop = True
                    else:
                        line = self.get_input_line(self.prompt)
                    self.statement.write(line)
                    if self.onecmd(self.statement.getvalue()):
                        self.reset_statement()
                except EOFError:
                    self.handle_eof()
                except cql.Error, cqlerr:
                    self.printerr(str(cqlerr))
                except KeyboardInterrupt:
                    self.reset_statement()
                    print

    def onecmd(self, statementtext):
        """
        Returns true if the statement is complete and was handled (meaning it
        can be reset).
        """

        try:
            statements, in_batch = cqlruleset.cql_split_statements(statementtext)
        except pylexotron.LexingError, e:
            if self.show_line_nums:
                self.printerr('Invalid syntax at char %d' % (e.charnum,))
            else:
                self.printerr('Invalid syntax at line %d, char %d'
                              % (e.linenum, e.charnum))
            statementline = statementtext.split('\n')[e.linenum - 1]
            self.printerr('  %s' % statementline)
            self.printerr(' %s^' % (' ' * e.charnum))
            return True

        while statements and not statements[-1]:
            statements = statements[:-1]
        if not statements:
            return True
        if in_batch or statements[-1][-1][0] != 'endtoken':
            self.set_continue_prompt()
            return
        for st in statements:
            try:
                self.handle_statement(st, statementtext)
            except Exception, e:
                if self.debug:
                    import traceback
                    traceback.print_exc()
                else:
                    self.printerr(e)
        return True

    def handle_eof(self):
        if self.tty:
            print
        statement = self.statement.getvalue()
        if statement.strip():
            if not self.onecmd(statement):
                self.printerr('Incomplete statement at end of file')
        self.do_exit()

    def handle_statement(self, tokens, srcstr):
        # Concat multi-line statements and insert into history
        if readline is not None:
            nl_count = srcstr.count("\n")

            new_hist = srcstr.replace("\n", " ").rstrip()

            if nl_count > 1 and self.last_hist != new_hist:
                readline.add_history(new_hist)

            self.last_hist = new_hist
        cmdword = tokens[0][1]
        if cmdword == '?':
            cmdword = 'help'
        custom_handler = getattr(self, 'do_' + cmdword.lower(), None)
        if custom_handler:
            parsed = cqlruleset.cql_whole_parse_tokens(tokens, srcstr=srcstr,
                                                       startsymbol='cqlshCommand')
            if parsed and not parsed.remainder:
                # successful complete parse
                return custom_handler(parsed)
            else:
                return self.handle_parse_error(cmdword, tokens, parsed, srcstr)
        return self.perform_statement(cqlruleset.cql_extract_orig(tokens, srcstr))

    def handle_parse_error(self, cmdword, tokens, parsed, srcstr):
        if cmdword.lower() in ('select', 'insert', 'update', 'delete', 'truncate',
                               'create', 'drop', 'alter', 'grant', 'revoke',
                               'batch', 'list'):
            # hey, maybe they know about some new syntax we don't. type
            # assumptions won't work, but maybe the query will.
            return self.perform_statement(cqlruleset.cql_extract_orig(tokens, srcstr))
        if parsed:
            self.printerr('Improper %s command (problem at %r).' % (cmdword, parsed.remainder[0]))
        else:
            self.printerr('Improper %s command.' % cmdword)

    def do_use(self, parsed):
        ksname = parsed.get_binding('ksname')
        if self.perform_statement_untraced(parsed.extract_orig()):
            if ksname[0] == '"' and ksname[-1] == '"':
                self.current_keyspace = self.cql_unprotect_name(ksname)
            else:
                self.current_keyspace = ksname.lower()

    def do_select(self, parsed):
        ksname = parsed.get_binding('ksname')
        if ksname is not None:
            ksname = self.cql_unprotect_name(ksname)
        cfname = self.cql_unprotect_name(parsed.get_binding('cfname'))
        statement = parsed.extract_orig()
        with_default_limit = parsed.get_binding('limit') is None
        if with_default_limit:
            statement = "%s LIMIT %d;" % (statement[:-1], DEFAULT_SELECT_LIMIT)
        self.perform_statement(statement,
                               decoder=ErrorHandlingSchemaDecoder,
                               with_default_limit=with_default_limit)

    def perform_statement(self, statement, decoder=None, with_default_limit=False):
        if self.tracing_enabled:
            session_id = UUID(bytes=self.trace_next_query())
            result = self.perform_statement_untraced(statement,
                                                     decoder=decoder,
                                                     with_default_limit=with_default_limit)
            time.sleep(0.5) # trace writes are async so we wait a little.
            print_trace_session(self, self.cursor, session_id)
            return result
        else:
            return self.perform_statement_untraced(statement,
                                                   decoder=decoder,
                                                   with_default_limit=with_default_limit)

    def perform_statement_untraced(self, statement, decoder=None, with_default_limit=False):
        if not statement:
            return False
        trynum = 1
        while True:
            try:
                self.cursor.execute(statement, decoder=decoder)
                break
            except cql.IntegrityError, err:
                self.printerr("Attempt #%d: %s" % (trynum, str(err)))
                trynum += 1
                if trynum > self.num_retries:
                    return False
                time.sleep(1*trynum)
            except cql.ProgrammingError, err:
                self.printerr(str(err))
                return False
            except CQL_ERRORS, err:
                self.printerr(str(err))
                return False
            except Exception, err:
                import traceback
                self.printerr(traceback.format_exc())
                return False

        if statement[:6].lower() == 'select' or statement.lower().startswith("list"):
            self.print_result(self.cursor, with_default_limit)
        elif self.cursor.rowcount > 0:
            # CAS INSERT/UPDATE
            self.writeresult("")
            self.print_static_result(self.cursor)
        self.flush_output()
        return True

    def get_nametype(self, cursor, num):
        """
        Determine the Cassandra type of a column name from the current row of
        query results on the given cursor. The column in question is given by
        its zero-based ordinal number within the row.

        This is necessary to differentiate some things like ascii vs. blob hex.
        """

        return cursor.name_info[num][1]

    def print_result(self, cursor, with_default_limit):
        self.decoding_errors = []

        self.writeresult("")
        if cursor.rowcount != 0:
            self.print_static_result(cursor)
        self.writeresult("(%d rows)" % cursor.rowcount)
        self.writeresult("")

        if self.decoding_errors:
            for err in self.decoding_errors[:2]:
                self.writeresult(err.message(), color=RED)
            if len(self.decoding_errors) > 2:
                self.writeresult('%d more decoding errors suppressed.'
                                 % (len(self.decoding_errors) - 2), color=RED)

        if with_default_limit:
            if (self.is_count_result(cursor) and self.get_count(cursor) == DEFAULT_SELECT_LIMIT) \
                    or cursor.rowcount == DEFAULT_SELECT_LIMIT:
                self.writeresult("Default LIMIT of %d was used. "
                                 "Specify your own LIMIT clause to get more results."
                                 % DEFAULT_SELECT_LIMIT, color=RED)
                self.writeresult("")

    def is_count_result(self, cursor):
        return cursor.description == [(u'count', 'LongType', None, None, None, None, True)]

    def get_count(self, cursor):
        return lookup_casstype('LongType').deserialize(cursor.result[0][0].value)

    def print_static_result(self, cursor):
        colnames = [d[0] for d in cursor.description]
        colnames_t = [(name, self.get_nametype(cursor, n)) for (n, name) in enumerate(colnames)]
        formatted_names = [self.myformat_colname(name, nametype) for (name, nametype) in colnames_t]
        formatted_values = [map(self.myformat_value, self.decode_row(cursor, row), cursor.column_types) for row in cursor.result]
        if self.expand_enabled:
            self.print_formatted_result_vertically(formatted_names, formatted_values)
        else:
            self.print_formatted_result(formatted_names, formatted_values)

    def print_formatted_result(self, formatted_names, formatted_values):
        # determine column widths
        widths = [n.displaywidth for n in formatted_names]
        for fmtrow in formatted_values:
            for num, col in enumerate(fmtrow):
                widths[num] = max(widths[num], col.displaywidth)

        # print header
        header = ' | '.join(hdr.ljust(w, color=self.color) for (hdr, w) in zip(formatted_names, widths))
        self.writeresult(' ' + header.rstrip())
        self.writeresult('-%s-' % '-+-'.join('-' * w for w in widths))

        # print row data
        for row in formatted_values:
            line = ' | '.join(col.rjust(w, color=self.color) for (col, w) in zip(row, widths))
            self.writeresult(' ' + line)

        self.writeresult("")

    def print_formatted_result_vertically(self, formatted_names, formatted_values):
        max_col_width = max([n.displaywidth for n in formatted_names])
        max_val_width = max([n.displaywidth for row in formatted_values for n in row])

        # for each row returned, list all the column-value pairs
        for row_id, row in enumerate(formatted_values):
            self.writeresult("@ Row %d" % (row_id + 1))
            self.writeresult('-%s-' % '-+-'.join(['-' * max_col_width, '-' * max_val_width]))
            for field_id, field in enumerate(row):
                column = formatted_names[field_id].ljust(max_col_width, color=self.color)
                value = field.ljust(field.displaywidth, color=self.color)
                self.writeresult(' ' + " | ".join([column, value]))
            self.writeresult('')

    def emptyline(self):
        pass

    def parseline(self, line):
        # this shouldn't be needed
        raise NotImplementedError

    def complete(self, text, state):
        if readline is None:
            return
        if state == 0:
            try:
                self.completion_matches = self.find_completions(text)
            except Exception:
                if debug_completion:
                    import traceback
                    traceback.print_exc()
                else:
                    raise
        try:
            return self.completion_matches[state]
        except IndexError:
            return None

    def find_completions(self, text):
        curline = readline.get_line_buffer()
        prevlines = self.statement.getvalue()
        wholestmt = prevlines + curline
        begidx = readline.get_begidx() + len(prevlines)
        endidx = readline.get_endidx() + len(prevlines)
        stuff_to_complete = wholestmt[:begidx]
        return cqlruleset.cql_complete(stuff_to_complete, text, cassandra_conn=self,
                                       debug=debug_completion, startsymbol='cqlshCommand')

    def set_prompt(self, prompt):
        self.prompt = prompt

    def cql_protect_name(self, name):
        if isinstance(name, unicode):
            name = name.encode('utf8')
        return cqlruleset.maybe_escape_name(name)

    def cql_protect_names(self, names):
        return map(self.cql_protect_name, names)

    def cql_protect_value(self, value):
        return cqlruleset.escape_value(value)

    def cql_unprotect_name(self, namestr):
        if namestr is None:
            return
        return cqlruleset.dequote_name(namestr)

    def cql_unprotect_value(self, valstr):
        if valstr is not None:
            return cqlruleset.dequote_value(valstr)

    def print_recreate_keyspace(self, ksdef, out):
        stratclass = trim_if_present(ksdef.strategy_class, 'org.apache.cassandra.locator.')
        ksname = self.cql_protect_name(ksdef.name)
        out.write("CREATE KEYSPACE %s WITH replication = {\n" % ksname)
        out.write("  'class': %s" % self.cql_protect_value(stratclass))
        for opname, opval in ksdef.strategy_options.iteritems():
            out.write(",\n  %s: %s" % (self.cql_protect_value(opname),
                                       self.cql_protect_value(opval)))
        out.write("\n}")
        if not ksdef.durable_writes:
            out.write(" AND durable_writes = 'false'")
        out.write(';\n')

        cfs = self.get_columnfamily_names(ksname)
        if cfs:
            out.write('\nUSE %s;\n' % ksname)
            for cf in cfs:
                out.write('\n')
                # yes, cf might be looked up again. oh well.
                self.print_recreate_columnfamily(ksdef.name, cf, out)

    def print_recreate_columnfamily(self, ksname, cfname, out):
        """
        Output CQL commands which should be pasteable back into a CQL session
        to recreate the given table.

        Writes output to the given out stream.
        """
        layout = self.get_columnfamily_layout(ksname, cfname)
        cfname = self.cql_protect_name(layout.name)
        out.write("CREATE TABLE %s (\n" % cfname)

        for col in layout.columns:
            colname = self.cql_protect_name(col.name)
            coltype = col.cqltype

            # Reversed types only matter for clustering order, not column definitions
            if issubclass(coltype, ReversedType):
                coltype = coltype.subtypes[0]

            out.write("  %s %s" % (colname, coltype.cql_parameterized_type()))
            if col.is_static():
                out.write(" static")
            out.write(",\n")

        out.write("  PRIMARY KEY (")
        partkeynames = self.cql_protect_names(layout.partition_key_columns)

        # Changed to put parenthesis around one or more partition keys in CASSANDRA-7274
        partkey = "(%s)" % ', '.join(partkeynames)

        pk_parts = [partkey] + self.cql_protect_names(layout.clustering_key_columns)
        out.write(', '.join(pk_parts) + ')')

        out.write("\n)")
        joiner = 'WITH'

        if layout.is_compact_storage():
            out.write(' WITH COMPACT STORAGE')
            joiner = 'AND'

        # check if we need a CLUSTERING ORDER BY clause
        if layout.clustering_key_columns:
            # get a list of clustering component types
            if issubclass(layout.comparator, CompositeType):
                clustering_types = layout.comparator.subtypes
            else:
                clustering_types = [layout.comparator]

            # only write CLUSTERING ORDER clause of we have >= 1 DESC item
            if any(issubclass(t, ReversedType) for t in clustering_types):
                if layout.is_compact_storage():
                    out.write(' AND\n ')
                else:
                    out.write(' WITH')
                out.write(' CLUSTERING ORDER BY (')

                clustering_names = self.cql_protect_names(layout.clustering_key_columns)

                inner = []
                for colname, coltype in zip(clustering_names, clustering_types):
                    ordering = "DESC" if issubclass(coltype, ReversedType) else "ASC"
                    inner.append("%s %s" % (colname, ordering))
                out.write(", ".join(inner))

                out.write(")")
                joiner = "AND"

        cf_opts = []
        compaction_strategy = trim_if_present(getattr(layout, 'compaction_strategy_class'),
                                              'org.apache.cassandra.db.compaction.')
        for cql3option, layoutoption in cqlruleset.columnfamily_layout_options:
            if layoutoption is None:
                layoutoption = cql3option
            optval = getattr(layout, layoutoption, None)
            if optval is None:
                if layoutoption == 'bloom_filter_fp_chance':
                    if compaction_strategy == 'LeveledCompactionStrategy':
                        optval = 0.1
                    else:
                        optval = 0.01
                else:
                    continue
            elif layoutoption == 'compaction_strategy_class':
                optval = compaction_strategy
            cf_opts.append((cql3option, self.cql_protect_value(optval)))
        for cql3option, layoutoption, _ in cqlruleset.columnfamily_layout_map_options:
            if layoutoption is None:
                layoutoption = cql3option
            optmap = getattr(layout, layoutoption, {})
            if layoutoption == 'compression_parameters':
                compclass = optmap.get('sstable_compression')
                if compclass is not None:
                    optmap['sstable_compression'] = \
                            trim_if_present(compclass, 'org.apache.cassandra.io.compress.')
            if layoutoption == 'compaction_strategy_options':
                optmap['class'] = compaction_strategy

            cf_opts.append((cql3option, optmap))

        if cf_opts:
            for optname, optval in cf_opts:
                if isinstance(optval, dict):
                    optval = '{%s}' % ', '.join(['%s: %s' % (self.cql_protect_value(k),
                                                             self.cql_protect_value(v))
                                                 for (k, v) in optval.items()])
                out.write(" %s\n  %s=%s" % (joiner, optname, optval))
                joiner = 'AND'
        out.write(";\n")

        for col in [ c for c in layout.columns if c.index_name is not None ]:
            out.write('\n')
            if col.index_type != 'CUSTOM':
                out.write('CREATE INDEX %s ON %s (%s);\n'
                             % (col.index_name, cfname, self.cql_protect_name(col.name)))
            else:
                out.write("CREATE CUSTOM INDEX %s ON %s (%s) USING '%s';\n"
                             % (col.index_name,
                                cfname,
                                self.cql_protect_name(col.name),
                                col.index_options[u'class_name']))

    def describe_keyspaces(self):
        print
        cmd.Cmd.columnize(self, self.get_keyspace_names())
        print

    def describe_keyspace(self, ksname):
        print
        self.print_recreate_keyspace(self.get_keyspace(ksname), sys.stdout)
        print

    def describe_columnfamily(self, ksname, cfname):
        if ksname is None:
            ksname = self.current_keyspace
        print
        self.print_recreate_columnfamily(ksname, cfname, sys.stdout)
        print

    def describe_columnfamilies(self, ksname):
        print
        if ksname is None:
            for k in self.get_keyspaces():
                name = self.cql_protect_name(k.name)
                print 'Keyspace %s' % (name,)
                print '---------%s' % ('-' * len(name))
                cmd.Cmd.columnize(self, self.get_columnfamily_names(k.name))
                print
        else:
            cmd.Cmd.columnize(self, self.get_columnfamily_names(ksname))
            print

    def describe_cluster(self):
        print '\nCluster: %s' % self.get_cluster_name()
        p = trim_if_present(self.get_partitioner(), 'org.apache.cassandra.dht.')
        print 'Partitioner: %s' % p
        snitch = trim_if_present(self.get_snitch(), 'org.apache.cassandra.locator.')
        print 'Snitch: %s\n' % snitch
        if self.current_keyspace is not None \
        and self.current_keyspace != 'system':
            print "Range ownership:"
            ring = self.get_ring()
            for entry in ring:
                print ' %39s  [%s]' % (entry.start_token, ', '.join(entry.endpoints))
            print

    def describe_schema(self, include_system=False):
        print
        for k in self.get_keyspaces():
            if include_system or not k.name in SYSTEM_KEYSPACES:
                self.print_recreate_keyspace(k, sys.stdout)
                print

    def do_describe(self, parsed):
        """
        DESCRIBE [cqlsh only]

        (DESC may be used as a shorthand.)

          Outputs information about the connected Cassandra cluster, or about
          the data stored on it. Use in one of the following ways:

        DESCRIBE KEYSPACES

          Output the names of all keyspaces.

        DESCRIBE KEYSPACE [<keyspacename>]

          Output CQL commands that could be used to recreate the given
          keyspace, and the tables in it. In some cases, as the CQL interface
          matures, there will be some metadata about a keyspace that is not
          representable with CQL. That metadata will not be shown.

          The '<keyspacename>' argument may be omitted when using a non-system
          keyspace; in that case, the current keyspace will be described.

        DESCRIBE TABLES

          Output the names of all tables in the current keyspace, or in all
          keyspaces if there is no current keyspace.

        DESCRIBE TABLE <tablename>

          Output CQL commands that could be used to recreate the given table.
          In some cases, as above, there may be table metadata which is not
          representable and which will not be shown.

        DESCRIBE CLUSTER

          Output information about the connected Cassandra cluster, such as the
          cluster name, and the partitioner and snitch in use. When you are
          connected to a non-system keyspace, also shows endpoint-range
          ownership information for the Cassandra ring.

        DESCRIBE [FULL] SCHEMA

          Output CQL commands that could be used to recreate the entire (non-system) schema.
          Works as though "DESCRIBE KEYSPACE k" was invoked for each non-system keyspace
          k. Use DESCRIBE FULL SCHEMA to include the system keyspaces.
        """
        what = parsed.matched[1][1].lower()
        if what == 'keyspaces':
            self.describe_keyspaces()
        if what == 'keyspace':
            ksname = self.cql_unprotect_name(parsed.get_binding('ksname', ''))
            if not ksname:
                ksname = self.current_keyspace
                if ksname is None:
                    self.printerr('Not in any keyspace.')
                    return
            self.describe_keyspace(ksname)
        elif what in ('columnfamily', 'table'):
            ks = self.cql_unprotect_name(parsed.get_binding('ksname', None))
            cf = self.cql_unprotect_name(parsed.get_binding('cfname'))
            self.describe_columnfamily(ks, cf)
        elif what in ('columnfamilies', 'tables'):
            self.describe_columnfamilies(self.current_keyspace)
        elif what == 'cluster':
            self.describe_cluster()
        elif what == 'schema':
            self.describe_schema(False)
        elif what == 'full' and parsed.matched[2][1].lower() == 'schema':
            self.describe_schema(True)
    do_desc = do_describe

    def do_copy(self, parsed):
        r"""
        COPY [cqlsh only]

          COPY x FROM: Imports CSV data into a Cassandra table
          COPY x TO: Exports data from a Cassandra table in CSV format.

        COPY <table_name> [ ( column [, ...] ) ]
             FROM ( '<filename>' | STDIN )
             [ WITH <option>='value' [AND ...] ];

        COPY <table_name> [ ( column [, ...] ) ]
             TO ( '<filename>' | STDOUT )
             [ WITH <option>='value' [AND ...] ];

        Available options and defaults:

          DELIMITER=','    - character that appears between records
          QUOTE='"'        - quoting character to be used to quote fields
          ESCAPE='\'       - character to appear before the QUOTE char when quoted
          HEADER=false     - whether to ignore the first line
          NULL=''          - string that represents a null value
          ENCODING='utf8'  - encoding for CSV output (COPY TO only)

        When entering CSV data on STDIN, you can use the sequence "\."
        on a line by itself to end the data input.
        """
        ks = self.cql_unprotect_name(parsed.get_binding('ksname', None))
        if ks is None:
            ks = self.current_keyspace
            if ks is None:
                raise NoKeyspaceError("Not in any keyspace.")
        cf = self.cql_unprotect_name(parsed.get_binding('cfname'))
        columns = parsed.get_binding('colnames', None)
        if columns is not None:
            columns = map(self.cql_unprotect_name, columns)
        else:
            # default to all known columns
            columns = self.get_column_names(ks, cf)
        fname = parsed.get_binding('fname', None)
        if fname is not None:
            fname = os.path.expanduser(self.cql_unprotect_value(fname))
        copyoptnames = map(str.lower, parsed.get_binding('optnames', ()))
        copyoptvals = map(self.cql_unprotect_value, parsed.get_binding('optvals', ()))
        cleancopyoptvals  = [optval.decode('string-escape') for optval in copyoptvals]
        opts = dict(zip(copyoptnames, cleancopyoptvals))

        timestart = time.time()

        direction = parsed.get_binding('dir').upper()
        if direction == 'FROM':
            rows = self.perform_csv_import(ks, cf, columns, fname, opts)
            verb = 'imported'
        elif direction == 'TO':
            rows = self.perform_csv_export(ks, cf, columns, fname, opts)
            verb = 'exported'
        else:
            raise SyntaxError("Unknown direction %s" % direction)

        timeend = time.time()
        print "%d rows %s in %s." % (rows, verb, describe_interval(timeend - timestart))

    def perform_csv_import(self, ks, cf, columns, fname, opts):
        dialect_options = self.csv_dialect_defaults.copy()
        if 'quote' in opts:
            dialect_options['quotechar'] = opts.pop('quote')
        if 'escape' in opts:
            dialect_options['escapechar'] = opts.pop('escape')
        if 'delimiter' in opts:
            dialect_options['delimiter'] = opts.pop('delimiter')
        nullval = opts.pop('null', '')
        header = bool(opts.pop('header', '').lower() == 'true')
        if dialect_options['quotechar'] == dialect_options['escapechar']:
            dialect_options['doublequote'] = True
            del dialect_options['escapechar']
        if opts:
            self.printerr('Unrecognized COPY FROM options: %s'
                          % ', '.join(opts.keys()))
            return 0

        if fname is None:
            do_close = False
            print "[Use \. on a line by itself to end input]"
            linesource = self.use_stdin_reader(prompt='[copy] ', until=r'\.')
        else:
            do_close = True
            try:
                linesource = open(fname, 'rb')
            except IOError, e:
                self.printerr("Can't open %r for reading: %s" % (fname, e))
                return 0
        try:
            if header:
                linesource.next()
            layout = self.get_columnfamily_layout(ks, cf)
            rownum = -1
            reader = csv.reader(linesource, **dialect_options)
            for rownum, row in enumerate(reader):
                if len(row) != len(columns):
                    self.printerr("Record #%d (line %d) has the wrong number of fields "
                                  "(%d instead of %d)."
                                  % (rownum, reader.line_num, len(row), len(columns)))
                    return rownum
                if not self.do_import_row(columns, nullval, layout, row):
                    self.printerr("Aborting import at record #%d (line %d). "
                                  "Previously-inserted values still present."
                                  % (rownum, reader.line_num))
                    return rownum
        finally:
            if do_close:
                linesource.close()
            elif self.tty:
                print
        return rownum + 1

    def do_import_row(self, columns, nullval, layout, row):
        rowmap = {}
        for name, value in zip(columns, row):
            type = layout.get_column(name).cqltype
            if issubclass(type, ReversedType):
                type = type.subtypes[0]
            cqltype = type.cql_parameterized_type()

            if value != nullval:
                if cqltype in ('ascii', 'text', 'timestamp', 'inet'):
                    rowmap[name] = self.cql_protect_value(value)
                else:
                    rowmap[name] = value
            elif name in layout.clustering_key_columns and not type.empty_binary_ok:
                rowmap[name] = 'blobAs%s(0x)' % cqltype.title()
            else:
                rowmap[name] = 'null'
        return self.do_import_insert(layout, rowmap)

    def do_import_insert(self, layout, rowmap):
        # would be nice to be able to use a prepared query here, but in order
        # to use that interface, we'd need to have all the input as native
        # values already, reading them from text just like the various
        # Cassandra cql types do. Better just to submit them all as intact
        # CQL string literals and let Cassandra do its thing.
        query = 'INSERT INTO %s.%s (%s) VALUES (%s)' % (
            self.cql_protect_name(layout.keyspace_name),
            self.cql_protect_name(layout.columnfamily_name),
            ', '.join(self.cql_protect_names(rowmap.keys())),
            ', '.join(rowmap.values())
        )
        if self.debug:
            print 'Import using CQL: %s' % query
        return self.perform_statement_untraced(query)

    def perform_csv_export(self, ks, cf, columns, fname, opts):
        dialect_options = self.csv_dialect_defaults.copy()
        if 'quote' in opts:
            dialect_options['quotechar'] = opts.pop('quote')
        if 'escape' in opts:
            dialect_options['escapechar'] = opts.pop('escape')
        if 'delimiter' in opts:
            dialect_options['delimiter'] = opts.pop('delimiter')
        encoding = opts.pop('encoding', 'utf8')
        nullval = opts.pop('null', '')
        header = bool(opts.pop('header', '').lower() == 'true')
        if dialect_options['quotechar'] == dialect_options['escapechar']:
            dialect_options['doublequote'] = True
            del dialect_options['escapechar']

        if opts:
            self.printerr('Unrecognized COPY TO options: %s'
                          % ', '.join(opts.keys()))
            return 0

        if fname is None:
            do_close = False
            csvdest = sys.stdout
        else:
            do_close = True
            try:
                csvdest = open(fname, 'wb')
            except IOError, e:
                self.printerr("Can't open %r for writing: %s" % (fname, e))
                return 0
        try:
            self.prep_export_dump(ks, cf, columns)
            writer = csv.writer(csvdest, **dialect_options)
            if header:
                writer.writerow([d[0] for d in self.cursor.description])
            rows = 0
            while True:
                row = self.cursor.fetchone()
                if row is None:
                    break
                fmt = lambda v, t: \
                    format_value(v, t, output_encoding=encoding, nullval=nullval,
                                 time_format=self.display_time_format,
                                 float_precision=self.display_float_precision).strval
                writer.writerow(map(fmt, row, self.cursor.column_types))
                rows += 1
        finally:
            if do_close:
                csvdest.close()
        return rows

    def prep_export_dump(self, ks, cf, columns):
        if columns is None:
            columns = self.get_column_names(ks, cf)
        columnlist = ', '.join(self.cql_protect_names(columns))
        # this limit is pretty awful. would be better to use row-key-paging, so
        # that the dump could be pretty easily aborted if necessary, but that
        # can be kind of tricky with cql3. Punt for now, until the real cursor
        # API is added in CASSANDRA-4415.
        query = 'SELECT %s FROM %s.%s LIMIT 99999999' \
                % (columnlist, self.cql_protect_name(ks), self.cql_protect_name(cf))
        self.cursor.execute(query)

    def do_show(self, parsed):
        """
        SHOW [cqlsh only]

          Displays information about the current cqlsh session. Can be called in
          the following ways:

        SHOW VERSION

          Shows the version and build of the connected Cassandra instance, as
          well as the versions of the CQL spec and the Thrift protocol that
          the connected Cassandra instance understands.

        SHOW HOST

          Shows where cqlsh is currently connected.

        SHOW SESSION <sessionid>

          Pretty-prints the requested tracing session.
        """
        showwhat = parsed.get_binding('what').lower()
        if showwhat == 'version':
            self.get_connection_versions()
            self.show_version()
        elif showwhat == 'host':
            self.show_host()
        elif showwhat.startswith('session'):
            session_id = parsed.get_binding('sessionid').lower()
            self.show_session(session_id)
        else:
            self.printerr('Wait, how do I show %r?' % (showwhat,))

    def do_source(self, parsed):
        """
        SOURCE [cqlsh only]

        Executes a file containing CQL statements. Gives the output for each
        statement in turn, if any, or any errors that occur along the way.

        Errors do NOT abort execution of the CQL source file.

        Usage:

          SOURCE '<file>';

        That is, the path to the file to be executed must be given inside a
        string literal. The path is interpreted relative to the current working
        directory. The tilde shorthand notation ('~/mydir') is supported for
        referring to $HOME.

        See also the --file option to cqlsh.
        """
        fname = parsed.get_binding('fname')
        fname = os.path.expanduser(self.cql_unprotect_value(fname))
        try:
            f = open(fname, 'r')
        except IOError, e:
            self.printerr('Could not open %r: %s' % (fname, e))
            return
        subshell = Shell(self.hostname, self.port, self.transport_factory,
                         color=self.color, encoding=self.encoding, stdin=f,
                         tty=False, use_conn=self.conn, cqlver=self.cql_version,
                         display_time_format=self.display_time_format,
                         display_float_precision=self.display_float_precision)
        subshell.cmdloop()
        f.close()

    def do_capture(self, parsed):
        """
        CAPTURE [cqlsh only]

        Begins capturing command output and appending it to a specified file.
        Output will not be shown at the console while it is captured.

        Usage:

          CAPTURE '<file>';
          CAPTURE OFF;
          CAPTURE;

        That is, the path to the file to be appended to must be given inside a
        string literal. The path is interpreted relative to the current working
        directory. The tilde shorthand notation ('~/mydir') is supported for
        referring to $HOME.

        Only query result output is captured. Errors and output from cqlsh-only
        commands will still be shown in the cqlsh session.

        To stop capturing output and show it in the cqlsh session again, use
        CAPTURE OFF.

        To inspect the current capture configuration, use CAPTURE with no
        arguments.
        """
        fname = parsed.get_binding('fname')
        if fname is None:
            if self.shunted_query_out is not None:
                print "Currently capturing query output to %r." % (self.query_out.name,)
            else:
                print "Currently not capturing query output."
            return

        if fname.upper() == 'OFF':
            if self.shunted_query_out is None:
                self.printerr('Not currently capturing output.')
                return
            self.query_out.close()
            self.query_out = self.shunted_query_out
            self.color = self.shunted_color
            self.shunted_query_out = None
            del self.shunted_color
            return

        if self.shunted_query_out is not None:
            self.printerr('Already capturing output to %s. Use CAPTURE OFF'
                          ' to disable.' % (self.query_out.name,))
            return

        fname = os.path.expanduser(self.cql_unprotect_value(fname))
        try:
            f = open(fname, 'a')
        except IOError, e:
            self.printerr('Could not open %r for append: %s' % (fname, e))
            return
        self.shunted_query_out = self.query_out
        self.shunted_color = self.color
        self.query_out = f
        self.color = False
        print 'Now capturing query output to %r.' % (fname,)

    def do_tracing(self, parsed):
        """
        TRACING [cqlsh]

          Enables or disables request tracing.

        TRACING ON

          Enables tracing for all further requests.

        TRACING OFF

          Disables tracing.

        TRACING

          TRACING with no arguments shows the current tracing status.
        """
        switch = parsed.get_binding('switch')
        if switch is None:
            if self.tracing_enabled:
                print "Tracing is currently enabled. Use TRACING OFF to disable"
            else:
                print "Tracing is currently disabled. Use TRACING ON to enable."
            return

        if switch.upper() == 'ON':
            if self.tracing_enabled:
                self.printerr('Tracing is already enabled. '
                              'Use TRACING OFF to disable.')
                return
            self.tracing_enabled = True
            print 'Now tracing requests.'
            return

        if switch.upper() == 'OFF':
            if not self.tracing_enabled:
                self.printerr('Tracing is not enabled.')
                return
            self.tracing_enabled = False
            print 'Disabled tracing.'

    def do_expand(self, parsed):
        """
        EXPAND [cqlsh]

          Enables or disables expanded (vertical) output.

        EXPAND ON

          Enables expanded (vertical) output.

        EXPAND OFF

          Disables expanded (vertical) output.

        EXPAND

          EXPAND with no arguments shows the current value of expand setting.
        """
        switch = parsed.get_binding('switch')
        if switch is None:
            if self.expand_enabled:
                print "Expanded output is currently enabled. Use EXPAND OFF to disable"
            else:
                print "Expanded output is currently disabled. Use EXPAND ON to enable."
            return

        if switch.upper() == 'ON':
            if self.expand_enabled:
                self.printerr('Expanded output is already enabled. '
                              'Use EXPAND OFF to disable.')
                return
            self.expand_enabled = True
            print 'Now printing expanded output'
            return

        if switch.upper() == 'OFF':
            if not self.expand_enabled:
                self.printerr('Expanded output is not enabled.')
                return
            self.expand_enabled = False
            print 'Disabled expanded output.'

    def do_consistency(self, parsed):
        """
        CONSISTENCY [cqlsh only]

           Overrides default consistency level (default level is ONE).

        CONSISTENCY <level>

           Sets consistency level for future requests.

           Valid consistency levels:

           ANY, ONE, TWO, THREE, QUORUM, ALL, LOCAL_ONE, LOCAL_QUORUM and EACH_QUORUM.

        CONSISTENCY

           CONSISTENCY with no arguments shows the current consistency level.
        """
        level = parsed.get_binding('level')
        if level is None:
            print 'Current consistency level is %s.' % (self.cursor.consistency_level,)
            return

        self.cursor.consistency_level = level.upper()
        print 'Consistency level set to %s.' % (level.upper(),)

    def do_exit(self, parsed=None):
        """
        EXIT/QUIT [cqlsh only]

        Exits cqlsh.
        """
        self.stop = True
    do_quit = do_exit

    def do_debug(self, parsed):
        import pdb
        pdb.set_trace()

    def get_help_topics(self):
        topics = [ t[3:] for t in dir(self) if t.startswith('do_') and getattr(self, t, None).__doc__]
        for hide_from_help in ('quit',):
            topics.remove(hide_from_help)
        return topics

    def columnize(self, slist, *a, **kw):
        return cmd.Cmd.columnize(self, sorted([u.upper() for u in slist]), *a, **kw)

    def do_help(self, parsed):
        """
        HELP [cqlsh only]

        Gives information about cqlsh commands. To see available topics,
        enter "HELP" without any arguments. To see help on a topic,
        use "HELP <topic>".
        """
        topics = parsed.get_binding('topic', ())
        if not topics:
            shell_topics = [ t.upper() for t in self.get_help_topics() ]
            self.print_topics("\nDocumented shell commands:", shell_topics, 15, 80)
            cql_topics = [ t.upper() for t in cqldocs.get_help_topics() ]
            self.print_topics("CQL help topics:", cql_topics, 15, 80)
            return
        for t in topics:
            if t.lower() in self.get_help_topics():
                doc = getattr(self, 'do_' + t.lower()).__doc__
                self.stdout.write(doc + "\n")
            elif t.lower() in cqldocs.get_help_topics():
                cqldocs.print_help_topic(t)
            else:
                self.printerr("*** No help on %s" % (t,))

    def applycolor(self, text, color=None):
        if not color or not self.color:
            return text
        return color + text + ANSI_RESET

    def writeresult(self, text, color=None, newline=True, out=None):
        if out is None:
            out = self.query_out
        out.write(self.applycolor(str(text), color) + ('\n' if newline else ''))

    def flush_output(self):
        self.query_out.flush()

    def printerr(self, text, color=RED, newline=True, shownum=None):
        self.statement_error = True
        if shownum is None:
            shownum = self.show_line_nums
        if shownum:
            text = '%s:%d:%s' % (self.stdin.name, self.lineno, text)
        self.writeresult(text, color, newline=newline, out=sys.stderr)

class ErrorHandlingSchemaDecoder(cql.decoders.SchemaDecoder):
    def name_decode_error(self, err, namebytes, expectedtype):
        return DecodeError(namebytes, err, expectedtype)

    def value_decode_error(self, err, namebytes, valuebytes, expectedtype):
        return DecodeError(valuebytes, err, expectedtype, colname=namebytes)

def option_with_default(cparser_getter, section, option, default=None):
    try:
        return cparser_getter(section, option)
    except ConfigParser.Error:
        return default

def raw_option_with_default(configs, section, option, default=None):
    """
    Same (almost) as option_with_default() but won't do any string interpolation.
    Useful for config values that include '%' symbol, e.g. time format string.
    """
    try:
        return configs.get(section, option, raw=True)
    except ConfigParser.Error:
        return default

def should_use_color():
    if not sys.stdout.isatty():
        return False
    if os.environ.get('TERM', '') in ('dumb', ''):
        return False
    try:
        import subprocess
        p = subprocess.Popen(['tput', 'colors'], stdout=subprocess.PIPE)
        stdout, _ = p.communicate()
        if int(stdout.strip()) < 8:
            return False
    except (OSError, ImportError, ValueError):
        # oh well, we tried. at least we know there's a $TERM and it's
        # not "dumb".
        pass
    return True

def load_factory(name):
    """
    Attempts to load a transport factory function given its fully qualified
    name, e.g. "cqlshlib.tfactory.regular_transport_factory"
    """
    parts = name.split('.')
    module = ".".join(parts[:-1])
    try:
        t = __import__(module)
        for part in parts[1:]:
            t = getattr(t, part)
        return t
    except (ImportError, AttributeError):
        sys.exit("Can't locate transport factory function %s" % name)

def read_options(cmdlineargs, environment):
    configs = ConfigParser.SafeConfigParser()
    configs.read(CONFIG_FILE)

    optvalues = optparse.Values()
    optvalues.username = option_with_default(configs.get, 'authentication', 'username')
    optvalues.password = option_with_default(configs.get, 'authentication', 'password')
    optvalues.keyspace = option_with_default(configs.get, 'authentication', 'keyspace')
    optvalues.transport_factory = option_with_default(configs.get, 'connection', 'factory',
                                                      DEFAULT_TRANSPORT_FACTORY)
    optvalues.completekey = option_with_default(configs.get, 'ui', 'completekey',
                                                DEFAULT_COMPLETEKEY)
    optvalues.color = option_with_default(configs.getboolean, 'ui', 'color')
    optvalues.time_format = raw_option_with_default(configs, 'ui', 'time_format',
                                                    DEFAULT_TIME_FORMAT)
    optvalues.float_precision = option_with_default(configs.getint, 'ui', 'float_precision',
                                                    DEFAULT_FLOAT_PRECISION)
    optvalues.debug = False
    optvalues.file = None
    optvalues.tty = sys.stdin.isatty()
    optvalues.cqlversion = option_with_default(configs.get, 'cql', 'version', DEFAULT_CQLVER)
    optvalues.execute = None

    (options, arguments) = parser.parse_args(cmdlineargs, values=optvalues)

    hostname = option_with_default(configs.get, 'connection', 'hostname', DEFAULT_HOST)
    port = option_with_default(configs.get, 'connection', 'port', DEFAULT_PORT)

    hostname = environment.get('CQLSH_HOST', hostname)
    port = environment.get('CQLSH_PORT', port)

    if len(arguments) > 0:
        hostname = arguments[0]
    if len(arguments) > 1:
        port = arguments[1]

    if options.file or options.execute:
        options.tty = False

    if options.execute and not options.execute.endswith(';'):
        options.execute += ';'

    options.transport_factory = load_factory(options.transport_factory)

    if optvalues.color in (True, False):
        options.color = optvalues.color
    else:
        if options.file is not None:
            options.color = False
        else:
            options.color = should_use_color()

    options.cqlversion, cqlvertup = full_cql_version(options.cqlversion)
    if cqlvertup[0] < 3:
        parser.error('%r is not a supported CQL version.' % options.cqlversion)
    else:
        options.cqlmodule = cql3handling

    try:
        port = int(port)
    except ValueError:
        parser.error('%r is not a valid port number.' % port)

    return options, hostname, port

def setup_cqlruleset(cqlmodule):
    global cqlruleset
    cqlruleset = cqlmodule.CqlRuleSet
    cqlruleset.append_rules(cqlsh_extra_syntax_rules)
    for rulename, termname, func in cqlsh_syntax_completers:
        cqlruleset.completer_for(rulename, termname)(func)
    cqlruleset.commands_end_with_newline.update(my_commands_ending_with_newline)

def setup_cqldocs(cqlmodule):
    global cqldocs
    cqldocs = cqlmodule.cqldocs

def init_history():
    if readline is not None:
        try:
            readline.read_history_file(HISTORY)
        except IOError:
            pass
        delims = readline.get_completer_delims()
        delims.replace("'", "")
        delims += '.'
        readline.set_completer_delims(delims)

def save_history():
    if readline is not None:
        try:
            readline.write_history_file(HISTORY)
        except IOError:
            pass

def main(options, hostname, port):
    setup_cqlruleset(options.cqlmodule)
    setup_cqldocs(options.cqlmodule)
    init_history()

    if options.file is None:
        stdin = None
    else:
        try:
            stdin = open(options.file, 'r')
        except IOError, e:
            sys.exit("Can't open %r: %s" % (options.file, e))

    if options.debug:
        import thrift
        sys.stderr.write("Using CQL driver: %s\n" % (cql,))
        sys.stderr.write("Using thrift lib: %s\n" % (thrift,))

    try:
        shell = Shell(hostname,
                      port,
                      options.transport_factory,
                      color=options.color,
                      username=options.username,
                      password=options.password,
                      stdin=stdin,
                      tty=options.tty,
                      completekey=options.completekey,
                      cqlver=options.cqlversion,
                      keyspace=options.keyspace,
                      display_time_format=options.time_format,
                      display_float_precision=options.float_precision,
                      single_statement=options.execute)
    except KeyboardInterrupt:
        sys.exit('Connection aborted.')
    except CQL_ERRORS, e:
        sys.exit('Connection error: %s' % (e,))
    except VersionNotSupported, e:
        sys.exit('Unsupported CQL version: %s' % (e,))
    if options.debug:
        shell.debug = True

    shell.cmdloop()
    save_history()
    batch_mode = options.file or options.execute
    if batch_mode and shell.statement_error:
        sys.exit(2)

if __name__ == '__main__':
    main(*read_options(sys.argv[1:], os.environ))

# vim: set ft=python et ts=4 sw=4 :
