## This exports methods available for use by plugins for sos

## Copyright (C) 2006 Steve Conklin <sconklin@redhat.com>

### This program is free software; you can redistribute it and/or modify
## it under the terms of the GNU General Public License as published by
## the Free Software Foundation; either version 2 of the License, or
## (at your option) any later version.

## This program is distributed in the hope that it will be useful,
## but WITHOUT ANY WARRANTY; without even the implied warranty of
## MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
## GNU General Public License for more details.

## You should have received a copy of the GNU General Public License
## along with this program; if not, write to the Free Software
## Foundation, Inc., 675 Mass Ave, Cambridge, MA 02139, USA.

# pylint: disable-msg = R0902
# pylint: disable-msg = R0904
# pylint: disable-msg = W0702
# pylint: disable-msg = W0703
# pylint: disable-msg = R0201
# pylint: disable-msg = W0611
# pylint: disable-msg = W0613
from __future__ import with_statement

from sos.utilities import sos_get_command_output, import_module, grep, fileobj, tail
import os
import glob
import re
import traceback
from stat import *
from time import time
from itertools import *
import logging
import fnmatch

# PYCOMPAT
import six
from six.moves import zip, filter

try:
    import json
except ImportError:
    import simplejson as json

def regex_findall(regex, fname):
    '''Return a list of all non overlapping matches in the string(s)'''
    try:
        with fileobj(fname) as f:
            return re.findall(regex, f.read(), re.MULTILINE)
    except AttributeError:
        return []

def mangle_command(command):
    # FIXME: this can be improved
    mangledname = re.sub(r"^/(usr/|)(bin|sbin)/", "", command)
    mangledname = re.sub(r"[^\w\-\.\/]+", "_", mangledname)
    mangledname = re.sub(r"/", ".", mangledname).strip(" ._-")[0:64]
    return mangledname

def _path_in_path_list(path, path_list):
    return any(p in path for p in path_list)

class Plugin(object):
    """ This is the base class for sosreport plugins. Plugins should subclass
    this and set the class variables where applicable.

    plugin_name is a string returned by plugin.name(). If this is set to None
    (the default) class_.__name__.tolower() will be returned. Be sure to set
    this if you are defining multiple plugins that do the same thing on
    different platforms.

    requires_root is a boolean that specifies whether or not sosreport should
    execute this plugin as a super user.

    version is a string representing the version of the plugin. This can be
    useful for post-collection tooling.

    packages (files) is an iterable of the names of packages (the paths
    of files) to check for before running this plugin. If any of these packages
    or files is found on the system, the default implementation of check_enabled
    will return True.
    """

    plugin_name = None
    requires_root = True
    version = 'unversioned'
    packages = ()
    files = ()

    def __init__(self, commons):
        if not getattr(self, "option_list", False):
            self.option_list = []

        self.copied_files = []
        self.executed_commands = []
        self.alerts = []
        self.custom_text = ""
        self.opt_names = []
        self.opt_parms = []
        self.commons = commons
        self.forbidden_paths = []
        self.copy_paths = set()
        self.copy_strings = []
        self.collect_cmds = []

        self.soslog = self.commons['soslog'] if 'soslog' in self.commons else logging.getLogger('sos')

        # get the option list into a dictionary
        for opt in self.option_list:
            self.opt_names.append(opt[0])
            self.opt_parms.append({'desc':opt[1], 'speed':opt[2], 'enabled':opt[3]})

    @classmethod
    def name(class_):
        """Returns the plugin's name as a string. This should return a
        lowercase string.
        """
        if class_.plugin_name:
            return class_.plugin_name
        return class_.__name__.lower()

    def _format_msg(self,msg):
        return "[plugin:%s] %s" % (self.name(), msg)

    def log_error(self, msg):
        self.soslog.error(self._format_msg(msg))

    def log_warn(self,msg):
        self.soslog.warning(self._format_msg(msg))

    def log_info(self, msg):
        self.soslog.info(self._format_msg(msg))

    def log_debug(self, msg):
        self.soslog.debug(self._format_msg(msg))

    def policy(self):
        return self.commons["policy"]

    def is_installed(self, package_name):
        '''Is the package $package_name installed?'''
        return (self.policy().pkg_by_name(package_name) is not None)

    def do_cmd_output_sub(self, cmd, regexp, subst):
        '''Apply a regexp substitution to command output archived by sosreport.
        cmd is the command name from which output is collected (i.e. excluding
        parameters). The regexp can be a string or a compiled re object. The
        substitution string, subst, is a string that replaces each occurrence
        of regexp in each file collected from cmd. Internally 'cmd' is treated
        as a glob with a leading and trailing '*' and each matching file from
        the current module's command list is subjected to the replacement.

        This function returns the number of replacements made.
        '''
        globstr = '*' + cmd + '*'
        self.log_debug("substituting '%s' for '%s' in commands matching '%s'"
                    % (subst, regexp, globstr))

        if not self.executed_commands:
            return 0

        replacements = None
        try:
            for called in self.executed_commands:
                # was anything collected?
                if called['file'] == None:
                    continue
                if fnmatch.fnmatch(called['exe'], globstr):
                    path = os.path.join(self.commons['cmddir'], called['file'])
                    self.log_debug("applying substitution to '%s'" % path)
                    readable = self.archive.open_file(path)
                    result, replacements = re.subn(
                            regexp, subst, readable.read())
                    if replacements:
                        self.archive.add_string(result, path)

        except Exception as e:
            msg = "regex substitution failed for '%s' with: '%s'"
            self.log_error(msg % (called['exe'], e))
            replacements = None
        return replacements

    def do_file_sub(self, srcpath, regexp, subst):
        '''Apply a regexp substitution to a file archived by sosreport.
        srcpath is the path in the archive where the file can be found.  regexp
        can be a regexp string or a compiled re object.  subst is a string to
        replace each occurance of regexp in the content of srcpath.

        This function returns the number of replacements made.
        '''
        try:
            path = self._get_dest_for_srcpath(srcpath)
            self.log_debug("substituting '%s' for '%s' in '%s'"
                    % (subst, regexp, path))
            if not path:
                return 0
            readable = self.archive.open_file(path)
            result, replacements = re.subn(regexp, subst, readable.read())
            if replacements:
                self.archive.add_string(result, srcpath)
            else:
                replacements = 0
        except Exception as e:
            msg = "regex substitution failed for '%s' with: '%s'"
            self.log_error(msg % (path, e))
            replacements = 0
        return replacements

    def do_path_regex_sub(self, pathexp, regexp, subst):
        '''Apply a regexp substituation to a set of files archived by
        sos. The set of files to be substituted is generated by matching
        collected file pathnames against pathexp which may be a regular
        expression string or compiled re object. The portion of the file
        to be replaced is specified via regexp and the replacement string
        is passed in subst.'''
        match = pathexp.match
        file_list = [f for f in self.copied_files if match(f['srcpath'])]
        for file in file_list:
            self.do_file_sub(file['srcpath'], regexp, subst)

    def do_regex_find_all(self, regex, fname):
        return regex_findall(regex, fname)

    def copy_symlink(self, srcpath):
        # the target stored in the original symlink
        linkdest = os.readlink(srcpath)
        dest = os.path.join(os.path.dirname(srcpath), linkdest)
        # absolute path to the link target
        absdest = os.path.normpath(dest)
        # adjust the target used inside the report to always be relative
        if os.path.isabs(linkdest):
                reldest = os.path.relpath(linkdest,
                                os.path.dirname(srcpath))
                self.log_debug("made link target '%s' relative as '%s'"
                                % (linkdest, reldest))
        else:
                reldest = linkdest

        self.log_debug("copying link '%s' pointing to '%s' with isdir=%s"
                       % (srcpath, linkdest, os.path.isdir(absdest)))

        if os.path.isdir(absdest):
            self.log_debug("link '%s' is a directory, skipping..." % linkdest)
            return

        # use the relative target path in the tarball
        self.archive.add_link(reldest,srcpath)

        # copy the symlink target translating relative targets
        # to absolute paths to pass to do_copy_path.
        self.log_debug("normalized link target '%s' as '%s'"
                       % (linkdest, absdest))
        self.do_copy_path(absdest)

        self.copied_files.append({'srcpath':srcpath,
                                  'dstpath':srcpath,
                                  'symlink':"yes",
                                  'pointsto':linkdest})

    def copy_dir(self, srcpath):
        for afile in os.listdir(srcpath):
            self.do_copy_path(os.path.join(srcpath, afile), dest=None)

    def _get_dest_for_srcpath(self, srcpath):
        for copied in self.copied_files:
            if srcpath == copied["srcpath"]:
                return copied["dstpath"]
        return None

    def is_forbidden_path(self, path):
        return _path_in_path_list(path, self.forbidden_paths)

    # Methods for copying files and shelling out
    def do_copy_path(self, srcpath, dest=None):
        # pylint: disable-msg = R0912
        # pylint: disable-msg = R0915
        '''Copy file or directory to the destination tree. If a directory, then
        everything below it is recursively copied. A list of copied files are
        saved for use later in preparing a report.
        '''
        if self.is_forbidden_path(srcpath):
            self.log_debug("skipping forbidden path '%s'" % srcpath)
            return ''

        if not os.path.exists(srcpath):
            self.log_debug("path '%s' does not exist" % srcpath)
            return

        if not dest:
            dest = srcpath

        if os.path.islink(srcpath):
            self.copy_symlink(srcpath)
            return
        else:
            if os.path.isdir(srcpath):
                self.copy_dir(srcpath)
                return

        # if we get here, it's definitely a regular file (not a symlink or dir)
        self.log_debug("copying file '%s' to archive:'%s'" % (srcpath,dest))

        stat = os.stat(srcpath)
        # if not readable(srcpath)
        if not (stat.st_mode & 0o444):
            # FIXME: reflect permissions in archive
            self.archive.add_string("", dest)
        else:
            self.archive.add_file(srcpath, dest)

        self.copied_files.append({'srcpath':srcpath,
                                      'dstpath':dest,
                                      'symlink':"no"})

    def add_forbidden_path(self, forbiddenPath):
        """Specify a path to not copy, even if it's part of a copy_specs[]
        entry.
        """
        # Glob case handling is such that a valid non-glob is a reduced glob
        for filespec in glob.glob(forbiddenPath):
            self.forbidden_paths.append(filespec)

    def get_all_options(self):
        """return a list of all options selected"""
        return (self.opt_names, self.opt_parms)

    def set_option(self, optionname, value):
        '''set the named option to value.'''
        for name, parms in zip(self.opt_names, self.opt_parms):
            if name == optionname:
                parms['enabled'] = value
                return True
        else:
            return False

    def option_enabled(self, optionname):
        '''Deprecated, use get_option() instead'''
        return self.get_option(optionname)

    def get_option(self, optionname, default=0):
        """Returns the first value that matches 'optionname' in parameters
        passed in via the command line or set via set_option or via the
        global_plugin_options dictionary, in that order.

        optionaname may be iterable, in which case the first option that
        matches any of the option names is returned.
        """

        def _check(key):
            if hasattr(optionname, "__iter__"):
                return key in optionname
            else:
                return key == optionname

        for name, parms in zip(self.opt_names, self.opt_parms):
            if _check(name):
                val = parms['enabled']
                if val != None:
                    return val

        items = six.iteritems(self.commons.get('global_plugin_options', {}))
        for key, value in items:
            if _check(key):
                return value

        return default

    def get_option_as_list(self, optionname, delimiter=",", default=None):
        '''Will try to return the option as a list separated by the
        delimiter.
        '''
        option = self.get_option(optionname)
        try:
            opt_list = [opt.strip() for opt in option.split(delimiter)]
            return list(filter(None, opt_list))
        except Exception:
            return default

    def add_copy_spec_limit(self, copyspec, sizelimit=None, tailit=True):
        """Add a file or glob but limit it to sizelimit megabytes. If fname is
        a single file the file will be tailed to meet sizelimit. If the first
        file in a glob is too large it will be tailed to meet the sizelimit.
        """
        if not (copyspec and len(copyspec)):
            return False

        files = glob.glob(copyspec)
        files.sort()
        if len(files) == 0:
            return
        current_size = 0
        limit_reached = False
        sizelimit *= 1024 * 1024 # in MB
        _file = None

        for _file in files:
            current_size += os.stat(_file)[ST_SIZE]
            if sizelimit and current_size > sizelimit:
                limit_reached = True
                break
            self.add_copy_spec(_file)

        if limit_reached and tailit:
            file_name = _file

            if file_name[0] == os.sep:
                file_name = file_name.lstrip(os.sep)
            strfile = file_name.replace(os.path.sep, ".") + ".tailed"
            self.add_string_as_file(tail(_file, sizelimit), strfile)
            rel_path = os.path.relpath('/', os.path.dirname(_file))
            link_path = os.path.join(rel_path, 'sos_strings',
                                     self.name(), strfile)
            self.archive.add_link(link_path, _file)

    def add_copy_specs(self, copyspecs):
        if isinstance(copyspecs, six.string_types):
            raise TypeError("add_copy_specs called with string argument")
        for copyspec in copyspecs:
            self.add_copy_spec(copyspec)

    def add_copy_spec(self, copyspec):
        """Add a file specification (can be file, dir,or shell glob) to be
        copied into the sosreport by this module.
        """
        if not (copyspec and len(copyspec)):
            self.log_warn("added null or empty copy spec")
            return False
        copy_paths = self.expand_copy_spec(copyspec)
        self.copy_paths.update(copy_paths)
        self.log_debug("added copyspec '%s'" % copyspec)

    def get_command_output(self, prog, timeout=300, runat=None):
        result = sos_get_command_output(prog, timeout=timeout, runat=runat)
        if result['status'] == 124:
            self.log_warn("command '%s' timed out after %ds"
                    % (prog, timeout))
        if result['status'] == 127:
            self.log_debug("could not run '%s': command not found" % prog)
        return result

    def call_ext_prog(self, prog, timeout=300, runat=None):
        """Execute a command independantly of the output gathering part of
        sosreport.
        """
        # pylint: disable-msg = W0612
        return self.get_command_output(prog, timeout=timeout, runat=runat)

    def check_ext_prog(self, prog):
        """Execute a command independently of the output gathering part of
        sosreport and check the return code. Return True for a return code of 0
        and False otherwise.
        """
        return (self.call_ext_prog(prog)['status'] == 0)

    def add_cmd_outputs(self, cmds, timeout=300, runat=None):
        """Run a list of programs and collect the output"""
        if isinstance(cmds, six.string_types):
            raise TypeError("add_cmd_outputs called with string argument")
        for cmd in cmds:
            self.add_cmd_output(cmd, timeout=timeout, runat=runat)

    def add_cmd_output(self, exe, suggest_filename=None,
                       root_symlink=None, timeout=300,
                       runat=None):
        """Run a program and collect the output"""
        cmd = (exe, suggest_filename, root_symlink, timeout, runat)
        self.log_debug("packed command tuple: ('%s', '%s', '%s', %s, '%s')"
                       % cmd)
        self.collect_cmds.append(cmd)
        self.log_info("added cmd output '%s'" % exe)

    def get_cmd_output_path(self, name=None, make=True):
        """Return a path into which this module should store collected
        command output
        """
        cmd_output_path = os.path.join(self.archive.get_tmp_dir(),
                                        'sos_commands', self.name())
        if name:
            cmd_output_path = os.path.join(cmd_output_path, name)
        if make:
            os.makedirs(cmd_output_path)

        return cmd_output_path

    def file_grep(self, regexp, *fnames):
        """Returns lines matched in fnames, where fnames can either be
        pathnames to files to grep through or open file objects to grep through
        line by line.
        """
        return grep(regexp, *fnames)

    def mangle_command(self, exe):
        return mangle_command(exe)

    def make_command_filename(self, exe):
        """The internal function to build up a filename based on a command."""

        outfn = os.path.join(self.commons['cmddir'], self.name(), self.mangle_command(exe))

        # check for collisions
        if os.path.exists(outfn):
            inc = 2
            while True:
               newfn = "%s_%d" % (outfn, inc)
               if not os.path.exists(newfn):
                  outfn = newfn
                  break
               inc +=1

        return outfn

    def add_string_as_file(self, content, filename):
        """Add a string to the archive as a file named `filename`"""
        self.copy_strings.append((content, filename))
        self.log_debug("added string '%s' as '%s'" % (content,filename))

    def get_cmd_output_now(self, exe, suggest_filename=None,
                           root_symlink=False, timeout=300,
                           runat=None):
        """Execute a command and save the output to a file for inclusion in the
        report.
        """
        # pylint: disable-msg = W0612
        result = self.get_command_output(exe, timeout=timeout, runat=runat)
        if (result['status'] == 127):
            return None

        if suggest_filename:
            outfn = self.make_command_filename(suggest_filename)
        else:
            outfn = self.make_command_filename(exe)

        outfn_strip = outfn[len(self.commons['cmddir'])+1:]
        self.archive.add_string(result['output'], outfn)
        if root_symlink:
            self.archive.add_link(outfn, root_symlink)

        # save info for later
        self.executed_commands.append({'exe': exe, 'file':outfn_strip}) # save in our list
        self.commons['xmlreport'].add_command(cmdline=exe,
                                              exitcode=result['status'],
                                              f_stdout=outfn_strip)

        return os.path.join(self.archive.get_archive_path(), outfn)

    # For adding output
    def add_alert(self, alertstring):
        """Add an alert to the collection of alerts for this plugin. These
        will be displayed in the report
        """
        self.alerts.append(alertstring)

    def add_custom_text(self, text):
        """Append text to the custom text that is included in the report. This
        is freeform and can include html.
        """
        self.custom_text += text

    def expand_copy_spec(self, copyspec):
        return glob.glob(copyspec)

    def collect_copy_specs(self):
        for path in self.copy_paths:
                self.log_info("collecting path '%s'" % path)
                self.do_copy_path(path)

    def collect_cmd_output(self):
        for progs in zip(self.collect_cmds):
            prog, suggest_filename, root_symlink, timeout, runat = progs[0]
            self.log_debug("unpacked command tuple: "
                           + "('%s', '%s', '%s', %s, '%s')" % progs[0])
            self.log_info("collecting output of '%s'" % prog)
            self.get_cmd_output_now(prog, suggest_filename=suggest_filename,
                                    root_symlink=root_symlink,
                                    timeout=timeout, runat=runat)

    def collect_strings(self):
        for string, file_name in self.copy_strings:
            self.log_info("collecting string '%s' as '%s'"
                          % (string, file_name))
            try:
                self.archive.add_string(string,
                        os.path.join('sos_strings', self.name(), file_name))
            except Exception as e:
                self.log_debug("could not add string '%s': %s"
                               % (file_name, e))

    def collect(self):
        """Collect the data for a plugin."""
        self.collect_copy_specs()
        self.collect_cmd_output()
        self.collect_strings()

    def get_description(self):
        """ This function will return the description for the plugin"""
        try:
            return self.__doc__.strip()
        except:
            return "<no description available>"

    def check_enabled(self):
        """This method will be used to verify that a plugin should execute
        given the condition of the underlying environment. The default
        implementation will return True if neither class.files or
        class.packages is specified. If either are specified the plugin will
        check for the existence of any of the supplied files or packages and
        return True if any exist. It is encouraged to override this method if
        this behavior isn't applicable.
        """
        # some files or packages have been specified for this package
        if self.files or self.packages:
            if isinstance(self.files, six.string_types):
                self.files = [self.files]

            if isinstance(self.packages, six.string_types):
                self.packages = [self.packages]

            return (any(os.path.exists(fname) for fname in self.files) or
                    any(self.is_installed(pkg) for pkg in self.packages))
        return True

    def default_enabled(self):
        """This decides whether a plugin should be automatically loaded or
        only if manually specified in the command line."""
        return True

    def setup(self):
        """Collect the list of files declared by the plugin. This method
        may be overridden to add further copy_specs, forbidden_paths, and
        external programs if required.
        """
        self.add_copy_specs(list(self.files))

    def postproc(self):
        """Perform any postprocessing. To be replaced by a plugin if required.
        """
        pass

    def report(self):
        """ Present all information that was gathered in an html file that allows browsing
        the results.
        """
        # make this prettier
        html = '<hr/><a name="%s"></a>\n' % self.name()

        # Intro
        html = html + "<h2> Plugin <em>" + self.name() + "</em></h2>\n"

        # Files
        if len(self.copied_files):
            html = html + "<p>Files copied:<br><ul>\n"
            for afile in self.copied_files:
                html = html + '<li><a href="%s">%s</a>' % \
                        (".." + afile['dstpath'], afile['srcpath'])
                if (afile['symlink'] == "yes"):
                    html = html + " (symlink to %s)" % afile['pointsto']
                html = html + '</li>\n'
            html = html + "</ul></p>\n"

        # Command Output
        if len(self.executed_commands):
            html = html + "<p>Commands Executed:<br><ul>\n"
            # convert file name to relative path from our root
            # don't use relpath - these are HTML paths not OS paths.
            for cmd in self.executed_commands:
                if cmd["file"] and len(cmd["file"]):
                    cmd_rel_path =  "../" + self.commons['cmddir'] \
                            + "/" + cmd['file']
                    html = html + '<li><a href="%s">%s</a></li>\n' % \
                            (cmd_rel_path, cmd['exe'])
                else:
                    html = html + '<li>%s</li>\n' % (cmd['exe'])
            html = html + "</ul></p>\n"

        # Alerts
        if len(self.alerts):
            html = html + "<p>Alerts:<br><ul>\n"
            for alert in self.alerts:
                html = html + '<li>%s</li>\n' % alert
            html = html + "</ul></p>\n"

        # Custom Text
        if (self.custom_text != ""):
            html = html + "<p>Additional Information:<br>\n"
            html = html + self.custom_text + "</p>\n"

        return html


class RedHatPlugin(object):
    """Tagging class to indicate that this plugin works with Red Hat Linux"""
    pass

class UbuntuPlugin(object):
    """Tagging class to indicate that this plugin works with Ubuntu Linux"""
    pass

class DebianPlugin(object):
    """Tagging class to indicate that this plugin works with Debian Linux"""
    pass

class IndependentPlugin(object):
    """Tagging class that indicates this plugin can run on any platform"""
    pass

def import_plugin(name, superclasses=None):
    """Import name as a module and return a list of all classes defined in that
    module. superclasses should be a tuple of valid superclasses to import,
    this defaults to (Plugin,).
    """
    plugin_fqname = "sos.plugins.%s" % name
    if not superclasses:
        superclasses = (Plugin,)
    return import_module(plugin_fqname, superclasses)

# vim: et ts=4 sw=4
