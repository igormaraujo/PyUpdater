# --------------------------------------------------------------------------
# Copyright (c) 2016 Digital Sapphire
#
# Permission is hereby granted, free of charge, to any person obtaining
# a copy of this software and associated documentation files
# (the "Software"), to deal in the Software without restriction, including
# without limitation the rights to use, copy, modify, merge, publish,
# distribute, sublicense, and/or sell copies of the Software, and to permit
# persons to whom the Software is furnished to do so, subject to the
# following conditions:
#
# The above copyright notice and this permission notice shall be
# included in all copies or substantial portions of the Software.
#
# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF
# ANY KIND, EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED
# TO THE WARRANTIES OF MERCHANTABILITY, FITNESS FOR A
# PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT
# SHALL THE AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR
# ANY CLAIM, DAMAGES OR OTHER LIABILITY, WHETHER IN AN
# ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
# OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE
# OR OTHER DEALINGS IN THE SOFTWARE.
# --------------------------------------------------------------------------
from __future__ import unicode_literals
import io
import logging
import os
import shutil
import subprocess
import sys
import tarfile
import threading
import zipfile

from dsdev_utils.helpers import Version
from dsdev_utils.paths import ChDir, get_mac_dot_app_dir, remove_any
from dsdev_utils.system import get_system

from pyupdater import settings
from pyupdater.client.downloader import FileDownloader, get_hash
from pyupdater.client.patcher import Patcher
from pyupdater.package_handler.package import remove_previous_versions
from pyupdater.utils.exceptions import ClientError


log = logging.getLogger(__name__)


def _get_highest_version(name, plat, channel, easy_data):
    """Parses version file and returns the highest version number.

        Args:

           name (str): name of file to search for updates

           plat (str): the platform we are requesting for

           channel (str): the release channel

           easy_data (dict): data file to search

        Returns:

           (str) Highest version number
    """
    # We grab all keys and return the version corresponding to the
    # channel passed to this function
    version_key_alpha = '{}*{}*{}*{}'.format('latest', name, 'alpha', plat)
    version_key_beta = '{}*{}*{}*{}'.format('latest', name, 'beta', plat)
    version_key_stable = '{}*{}*{}*{}'.format('latest', name, 'stable', plat)
    version = None
    alpha = easy_data.get(version_key_alpha)
    if alpha is None:
        alpha = '0.0.0a0'

    beta = easy_data.get(version_key_beta)
    if beta is None:
        beta = '0.0.0b0'

    stable = easy_data.get(version_key_stable)

    if channel == 'alpha':
        version = alpha

    if channel == 'beta':
        version = beta

    if stable is not None and channel == 'stable':
        version = stable

    if version is not None:
        log.debug('Highest version: %s', version)
    else:
        log.info('No updates for "%s" on %s exists', name, plat)

    return version


def _gen_user_friendly_version(internal_version):
    channel = {0: 'Alpha', 1: 'Beta'}
    v = list(map(int, internal_version.split('.')))

    # 1.2
    version = '{}.{}'.format(v[0], v[1])
    if v[2] != 0:
        # 1.2.1
        version += '.{}'.format(v[2])
    if v[3] != 2:
        # 1.2.1 Alpha
        version += ' {}'.format(channel[v[3]])
        if v[4] != 0:
            version += ' {}'.format(v[4])

    return version


class Restarter(object):

    def __init__(self, current_app, **kwargs):
        self.current_app = current_app
        self.name = kwargs.get('name', "")
        log.debug('Current App: %s', self.current_app)
        self.is_win = sys.platform == 'win32'
        if self.is_win is True:
            self.data_dir = kwargs.get('data_dir')
            self.bat_file = os.path.join(self.data_dir, 'update.bat')
            self.vbs_file = os.path.join(self.data_dir, 'invis.vbs')
            self.updated_app = kwargs.get('updated_app')
            log.debug('Restart script dir: %s', self.data_dir)
            log.debug('Update path: %s', self.updated_app)

    def process(self, win_restart=True):
        if self.is_win:
            if win_restart is True:
                self._win_overwrite_restart()
            else:
                self._win_overwrite()
        else:
            self._restart()

    def _restart(self):
        os.execl(self.current_app, self.name)

    def _win_overwrite(self):
        with io.open(self.bat_file, 'w', encoding='utf-8') as bat:
            bat.write("""
@echo off
echo Updating to latest version...
ping 127.0.0.1 -n 5 -w 1000 > NUL
move /Y "{}" "{}" > NUL
DEL {}
DEL "%~f0"
""".format(self.updated_app, self.current_app, self.vbs_file))
        with io.open(self.vbs_file, 'w', encoding='utf-8') as vbs:
            # http://www.howtogeek.com/131597/can-i-run-a-windows-batch-file-without-a-visible-command-prompt/
            vbs.write('CreateObject("Wscript.Shell").Run """" '
                      '& WScript.Arguments(0) & """", 0, False')
        log.debug('Starting update batch file')
        args = ['wscript.exe', self.vbs_file, self.bat_file]
        subprocess.Popen(args)
        sys.exit(0)

    def _win_overwrite_restart(self):
        with io.open(self.bat_file, 'w', encoding='utf-8') as bat:
            bat.write("""
@echo off
echo Updating to latest version...
ping 127.0.0.1 -n 5 -w 1000 > NUL
move /Y "{}" "{}" > NUL
echo restarting...
start "" "{}"
DEL {}
DEL "%~f0"
""".format(self.updated_app, self.current_app,
                self.current_app, self.vbs_file))

        with io.open(self.vbs_file, 'w', encoding='utf-8') as vbs:
            # http://www.howtogeek.com/131597/can-i-run-a-windows-batch-file-without-a-visible-command-prompt/
            vbs.write('CreateObject("Wscript.Shell").Run """" '
                      '& WScript.Arguments(0) & """", 0, False')
        log.debug('Starting update batch file')
        args = ['wscript.exe', self.vbs_file, self.bat_file]
        subprocess.Popen(args)
        sys.exit(0)


class LibUpdate(object):
    """Used to update library files used by an application. This object is
    returned by pyupdater.client.Client.update_check

    ######Args:

    data (dict): Info dict
    """

    def __init__(self, data=None):
        self._updates_key = settings.UPDATES_KEY
        self._current_app_dir = os.path.dirname(sys.executable)
        self._status = False
        # If user is using async download this will be True.
        # Future calls to an download methods will not run
        # until the current download is complete. Which will
        # set this back to False.
        self._is_downloading = False
        self._version = ""

        if data is not None:
            self._init_app(data)

    def _init_app(self, data):
        self.init_data = data
        self.update_urls = data.get('update_urls')
        self.name = data.get('name')
        self.current_version = data.get('version')
        self.easy_data = data.get('easy_data')
        # Raw form of easy_data
        self.json_data = data.get('json_data')
        self.data_dir = data.get('data_dir')
        self.platform = data.get('platform')
        self.app_name = data.get('app_name')
        self.channel = data.get('channel', 'stable')
        self.progress_hooks = data.get('progress_hooks')
        self.update_folder = os.path.join(self.data_dir,
                                          settings.UPDATE_FOLDER)
        self.verify = data.get('verify', True)
        self.max_download_retries = data.get('max_download_retries')

        # Used to generate file name of archive
        self.latest = _get_highest_version(self.name, self.platform,
                                           self.channel, self.easy_data)

        self._current_archive_name = self._get_filename(self.name,
                                                        self.current_version,
                                                        self.platform,
                                                        self.easy_data)

        # Get full filename of latest update archive
        self.filename = self._get_filename(self.name, self.latest,
                                           self.platform, self.easy_data)
        assert self.filename is not None

        # Removes old versions, of this asset, from
        # the updates folder.
        self.cleanup()

    @property
    def version(self):
        if self._version == "":
            self._version = _gen_user_friendly_version(self.latest)
        return self._version

    def is_downloaded(self):
        """Used to check if update has been downloaded.

        ######Returns (bool): True - File is already downloaded.
        False - File hasn't been downloaded.
        """
        if self._is_downloading is True:
            return False
        return self._is_downloaded()

    def download(self, async=False):
        """Downloads update

        ######Args:

        async (bool): Perform download in background thread
        """
        if async is True:
            if self._is_downloading is False:
                self._is_downloading = True
                threading.Thread(target=self._download).start()
        else:
            if self._is_downloading is False:
                self._is_downloading = True
                return self._download()

    def extract(self):
        """Will extract the update from its archive to the update folder.
        If updating a lib you can take over from there. If updating
        an app this call should be followed by method "restart" to
        complete update.

        ######Returns:

        (bool) True - Extract successful. False - Extract failed.
        """
        if get_system() == 'win':  # Tested elsewhere
            log.debug('Only supported on Unix like systems')
            return False
        try:
            self._extract_update()
        except ClientError as err:
            log.debug(err, exc_info=True)
            return False
        return True

    def _get_filename(self, name, version, platform, easy_data):
        """Gets full filename for given name & version combo

            Args:

                name (str): name of file to get full filename for

               version (str): version of file to get full filename for

               easy_data (dict): data file to search

            Returns:

               (str) Filename with extension
        """
        filename_key = '{}*{}*{}*{}*{}'.format(settings.UPDATES_KEY, name,
                                               version, platform, 'filename')
        filename = easy_data.get(filename_key)

        log.debug("Filename for %s-%s: %s", name, version, filename)
        return filename

    def _download(self):
        """Will download the package update that was referenced
        with check update.

        Proxy method for method "_patch_update" & method "_full_update".

        Returns:

            (bool) Meanings:

                True - Download successful

                False - Download failed
        """
        if self.name is not None:
            if self._is_downloaded() is True:  # pragma: no cover
                self._status = True
            else:
                log.debug('Starting patch download')
                patch_success = False
                if self.channel == 'stable':
                    patch_success = self._patch_update()
                # Tested elsewhere
                if patch_success:  # pragma: no cover
                    self._status = True
                    log.debug('Patch download successful')
                else:
                    log.debug('Patch update failed')
                    log.debug('Starting full download')
                    update_success = self._full_update()
                    if update_success:
                        self._status = True
                        log.debug('Full download successful')
                    else:  # pragma: no cover
                        log.debug('Full download failed')

        self._is_downloading = False
        return self._status

    def _extract_update(self):
        with ChDir(self.update_folder):
            if not os.path.exists(self.filename):
                log.debug('File does not exists')
                raise ClientError('File does not exists', expected=True)

            verified = self._verify_file_hash()
            if verified:
                log.debug('Extracting Update')
                archive_ext = os.path.splitext(self.filename)[1].lower()

                if archive_ext == '.gz':
                    try:
                        with tarfile.open(self.filename, 'r:gz') as tfile:
                            # Extract file update to current
                            # directory.
                            tfile.extractall()
                    except Exception as err:  # pragma: no cover
                        log.debug(err, exc_info=True)
                        raise ClientError('Error reading gzip file')
                elif archive_ext == '.zip':
                    try:
                        with zipfile.ZipFile(self.filename, 'r') as zfile:
                            # Extract update file to current
                            # directory.
                            zfile.extractall()
                    except Exception as err:  # pragma: no cover
                        log.debug(err, exc_info=True)
                        raise ClientError('Error reading zip file')
                else:
                    raise ClientError('Unknown filetype')
            else:
                raise ClientError('Update archive is corrupt')

    def _get_file_hash_from_manifest(self):
        hash_key = '{}*{}*{}*{}*{}'.format(self._updates_key,
                                           self.name, self.latest,
                                           self.platform, 'file_hash')
        return self.easy_data.get(hash_key)

    # Must be called from directory where file is located
    def _verify_file_hash(self):
        if not os.path.exists(self.filename):
            log.debug('File does not exist')
            return False

        file_hash = self._get_file_hash_from_manifest()
        try:
            with open(self.filename, 'rb') as f:
                data = f.read()
        except Exception as err:
            log.debug(err, exc_info=True)
            return False

        if file_hash == get_hash(data):
            return True
        else:
            return False

    # Checks if latest update is already downloaded
    def _is_downloaded(self):
        # Comparing file hashes to ensure security
        with ChDir(self.update_folder):
            verified = self._verify_file_hash()

        return verified

    # Handles patch updates
    def _patch_update(self):  # pragma: no cover
        log.debug('Starting patch update')
        # The current version is not in the version manifest
        if self._current_archive_name is None:
            return False

        # Just checking to see if the zip for the current version is
        # available to patch If not we'll fall back to a full binary download
        if not os.path.exists(os.path.join(self.update_folder,
                                           self._current_archive_name)):
            log.debug('%s got deleted. No base binary to start patching '
                      'form', self._current_archive_name)
            return False

        # Initilize Patch object with all required information
        p = Patcher(current_version=self.current_version,
                    latest_version=self.latest,
                    update_folder=self.update_folder,
                    **self.init_data)

        # Returns True if everything went well
        # If False, fall back to a full update
        return p.start()

    def _full_update(self):
        log.debug('Starting full update')
        file_hash = self._get_file_hash_from_manifest()

        with ChDir(self.update_folder):
            log.debug('Downloading update...')
            fd = FileDownloader(self.filename, self.update_urls,
                                hexdigest=file_hash, verify=self.verify,
                                progress_hooks=self.progress_hooks,
                                max_download_retries=self.max_download_retries)
            result = fd.download_verify_write()
            if result:
                log.debug('Download Complete')
                return True
            else:  # pragma: no cover
                log.debug('Failed To Download Latest Version')
                return False

    def cleanup(self):
        """Cleans up old update archives for this app or asset"""
        log.debug('Beginning removal of old updates')
        remove_previous_versions(self.update_folder,
                                 self._current_archive_name)


class AppUpdate(LibUpdate):
    """Used to update an application. This object is returned by
    pyupdater.client.Client.update_check

    Args:

    data (dict): Info dict
    """

    def __init__(self, data):
        super(AppUpdate, self).__init__(data)

    def extract_restart(self):  # pragma: no cover
        """Will extract the update, overwrite the current binary,
        then restart the application using the updated binary."""
        try:
            self._extract_update()

            if get_system() == 'win':
                self._win_overwrite_restart()
            else:
                self._overwrite()
                self._restart()
        except ClientError as err:
            log.debug(err, exc_info=True)

    def extract_overwrite(self):  # pragma: no cover
        """Will extract the update then overwrite the current binary"""
        try:
            self._extract_update()
            if get_system() == 'win':
                self._win_overwrite()
            else:
                self._overwrite()
        except ClientError as err:
            log.debug(err, exc_info=True)

    # ToDo: Remove in v3.0
    def win_extract_overwrite(self):
        """Overwrite current binary with update bianry on windows.

        Deprecated: Use extract_overwrite instead.
        """
        self._win_overwrite()
    # End ToDo

    # ToDo: Remove in v3.0
    def restart(self):  # pragma: no cover
        """Will overwrite old binary with updated binary and
        restart the application using the updated binary.
        Not supported on windows.

        Deprecated: Used extract_restart instead.
        """
        # On windows we write a batch file to move the update
        # binary to the correct location and restart app.
        if get_system() == 'win':
            log.debug('Only supported on Unix like systems')
            return
        try:
            self._overwrite()
            self._restart()
        except ClientError as err:
            log.debug(err, exc_info=True)
    # End ToDo

    def _overwrite(self):  # pragma: no cover
        # Unix: Overwrites the running applications binary
        if get_system() == 'mac':
            if self._current_app_dir.endswith('MacOS') is True:
                log.debug('Looks like we\'re dealing with a Mac Gui')

                temp_dir = get_mac_dot_app_dir(self._current_app_dir)
                self._current_app_dir = temp_dir

        app_update = os.path.join(self.update_folder, self.name)

        # Must be dealing with Mac .app application
        if not os.path.exists(app_update):
            app_update += '.app'

        log.debug('Update Location:\n%s', os.path.dirname(app_update))
        log.debug('Update Name: %s', os.path.basename(app_update))

        current_app = os.path.join(self._current_app_dir, self.name)

        # Must be dealing with Mac .app application
        if not os.path.exists(current_app):
            current_app += '.app'

        log.debug('Current App location:\n\n%s', current_app)

        # Remove current app to prevent errors when moving
        # update to new location
        if os.path.exists(current_app):
            remove_any(current_app)

        log.debug('Moving app to new location:\n\n%s', self._current_app_dir)
        shutil.move(app_update, self._current_app_dir)

    def _restart(self):  # pragma: no cover
        log.debug('Restarting')
        current_app = os.path.join(self._current_app_dir, self.name)
        if get_system() == 'mac':
            # Must be dealing with Mac .app application
            if not os.path.exists(current_app):
                log.debug('Must be a .app bundle')
                current_app += '.app'
                mac_app_binary_dir = os.path.join(current_app, 'Contents',
                                                  'MacOS')
                _file = os.listdir(mac_app_binary_dir)

                # We are making an assumption here that only 1
                # executable will be in the MacOS folder.
                current_app = os.path.join(mac_app_binary_dir, _file[0])

        r = Restarter(current_app, name=self.name)
        r.process()

    def _win_overwrite(self):  # pragma: no cover
        # Windows: Moves update to current directory of running
        #                 application then restarts application using
        #                 new update.
        exe_name = self.name + '.exe'
        current_app = os.path.join(self._current_app_dir, exe_name)
        updated_app = os.path.join(self.update_folder, exe_name)

        update_info = dict(data_dir=self.data_dir, updated_app=updated_app)
        r = Restarter(current_app, **update_info)
        r.process(win_restart=False)

    def _win_overwrite_restart(self):  # pragma: no cover
        # Windows: Moves update to current directory of running
        #          application then restarts application using
        #          new update.
        exe_name = self.name + '.exe'
        current_app = os.path.join(self._current_app_dir, exe_name)
        updated_app = os.path.join(self.update_folder, exe_name)

        update_info = dict(data_dir=self.data_dir, updated_app=updated_app)
        r = Restarter(current_app, **update_info)
        r.process()
