sktm - skt manager
==================

sktm is an orchestrator for skt keeping track of known baselines and tested
patches.

Usage
-----
At the moment sktm works through jenkins thus requiring a running jenkins
instance and an account in that instance. Jenkins credentials can be passed
through cmdline arguments (see `--help`) or rc file (defaults to `~/.sktmrc`).
rcfile should look like this:

    [config]
    jurl = http://jenkins.baseurl.com:8080
    jlogin = mylogin
    jpass = mypassword
    jjname = jenkinsjobname

Before testing any patches you need to establish a stable baseline, to test a
specific ref run sktm like this:

    sktm.py -v baseline git://git.kernel.org/pub/scm/linux/kernel/git/davem/net-next.git master

If that passes - you now have a baseline to apply the patches to. sktm imports
patches from patchwork. By default all new patches since last run are tested,
but since you don't have any "last run" yet you'll have to supply
`--lastpatch` argument first time you import patches from a specific patchwork
project:

    sktm.py -v patchwork --lastpatch 839503 git://git.kernel.org/pub/scm/linux/kernel/git/davem/net-next.git https://patchwork.ozlabs.org netdev

Next time `--lastpatch` can be omitted.

License
-------
sktm is distributed under GPLv2 license.

This program is free software: you can redistribute it and/or modify
it under the terms of the GNU General Public License as published by
the Free Software Foundation, either version 2 of the License, or
(at your option) any later version.

This program is distributed in the hope that it will be useful,
but WITHOUT ANY WARRANTY; without even the implied warranty of
MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
GNU General Public License for more details.

You should have received a copy of the GNU General Public License
along with this program. If not, see <http://www.gnu.org/licenses/>.
