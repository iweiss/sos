## Copyright (C) 2007 Shijoe George <spanjikk@redhat.com>

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

from sos.plugins import Plugin, RedHatPlugin, DebianPlugin, UbuntuPlugin

class Nscd(Plugin, RedHatPlugin, DebianPlugin, UbuntuPlugin):
    """NSCD related information
    """

    plugin_name = 'nscd'

    option_list = [("nscdlogsize", "max size (MiB) to collect per nscd log file",
                   "", 50)]

    files = ('/etc/nscd.conf',)
    packages = ('nscd',)

    def setup(self):
        self.add_copy_spec("/etc/nscd.conf")

        opt = self.file_grep(r"^\s*logfile", "/etc/nscd.conf")
        if (len(opt) > 0):
            for o in opt:
                f = o.split()
                self.add_copy_spec_limit(f[1],
                    sizelimit = self.option_enabled("nscdlogsize"))

# vim: et ts=4 sw=4
