# This file is part of Buildbot. Buildbot is free software: you can
# redistribute it and/or modify it under the terms of the GNU General Public
# License as published by the Free Software Foundation, version 2.
#
# This program is distributed in the hope that it will be useful, but WITHOUT
# ANY WARRANTY; without even the implied warranty of MERCHANTABILITY or FITNESS
# FOR A PARTICULAR PURPOSE. See the GNU General Public License for more
# details.
#
# You should have received a copy of the GNU General Public License along with
# this program; if not, write to the Free Software Foundation, Inc., 51
# Franklin Street, Fifth Floor, Boston, MA 02110-1301 USA.
#
# Copyright Buildbot Team Members

# Needed so that this module name don't clash with docker-py on older python.
from __future__ import absolute_import

from io import BytesIO
import json

from twisted.internet import defer
from twisted.internet import threads
from twisted.python import log

from buildbot import config
from buildbot import interfaces
from buildbot.buildslave import AbstractLatentBuildSlave

try:
    from docker import client
    _hush_pyflakes = [client]
except ImportError:
    client = None

def handle_stream_line(line):
    """\
    Input is the json representation of: {'stream': "Content\ncontent"}
    Output is a generator yield "Content", and then "content"
    """
    # XXX This necessary processing is probably a bug from docker-py,
    # hence, might break if the bug is fixed ...
    line = json.loads(line)
    if 'error' in line:
        content = "ERROR: " + line['error']
    else:
        content = line.get('stream', '')
    for streamline in content.split('\n'):
        if streamline:
            yield streamline


class DockerLatentBuildSlave(AbstractLatentBuildSlave):
    instance = None

    def __init__(self, name, password, docker_host, image=None, command=None,
                 max_builds=None, notify_on_missing=None,
                 missing_timeout=(60 * 20), build_wait_timeout=0,
                 properties={}, locks=None, volumes=None, dockerfile=None,
                 version=None, tls=None):

        if not client:
            config.error("The python module 'docker-py' is needed to use a"
                         " DockerLatentBuildSlave")
        if not image and not dockerfile:
            config.error("DockerLatentBuildSlave: You need to specify at least"
                         " an image name, or a dockerfile")

        self.volumes = []
        self.binds = {}
        for volume_string in (volumes or []):
            try:
                volume, bind = volume_string.split(":", 1)
            except ValueError:
                config.error("Invalid volume definition for docker "
                             "{0}. Skipping...".format(volume_string))
            self.volumes.append(volume)

            ro = False
            if bind.endswith(':ro'):
                bind = bind[:-3]
                ro = True
            self.binds[volume] = {'bind': bind, 'ro': ro}

        AbstractLatentBuildSlave.__init__(self, name, password, max_builds,
                                          notify_on_missing or [],
                                          missing_timeout, build_wait_timeout,
                                          properties, locks)

        self.docker_host = docker_host
        self.image = image
        self.command = command or []

        self.dockerfile = dockerfile
        self.version = version
        self.tls = tls

    def start_instance(self, build):
        if self.instance is not None:
            raise ValueError('instance active')
        return threads.deferToThread(self._thd_start_instance)

    def _image_exists(self, client, name=None):
        if name is None:
            name = self.image
        # Make sure the container exists
        for image in client.images():
            for tag in image['RepoTags']:
                if ':' in name and tag == name:
                    return True
                if tag.startswith(name + ':'):
                    return True
        return False

    def _get_client_params(self):
        kwargs = {'base_url': self.docker_host}
        if self.version is not None:
            kwargs['version'] = self.version
        if self.tls is not None:
            kwargs['tls'] = self.tls
        return kwargs

    def _thd_start_instance(self):
        docker_client = client.Client(**self._get_client_params())

        found = False
        if self.image is not None:
            found = self._image_exists(docker_client)
            image = self.image
        else:
            image = '%s_%s_image' % (self.slavename, id(self))
        if (not found) and (self.dockerfile is not None):
            log.msg("Image '%s' not found, building it from scratch" %
                    image)
            for line in docker_client.build(fileobj=BytesIO(self.dockerfile.encode('utf-8')),
                                            tag=image):
                for streamline in handle_stream_line(line):
                    log.msg(streamline)

        if (not self._image_exists(docker_client, image)):
            log.msg("Image '%s' not found" % image)
            raise interfaces.LatentBuildSlaveFailedToSubstantiate(
                'Image "%s" not found on docker host.' % image
            )

        instance = docker_client.create_container(
            image,
            self.command,
            name='%s_%s' % (self.slavename, id(self)),
            volumes=self.volumes,
        )

        if instance.get('Id') is None:
            log.msg('Failed to create the container')
            raise interfaces.LatentBuildSlaveFailedToSubstantiate(
                'Failed to start container'
            )

        log.msg('Container created, Id: %s...' % instance['Id'][:6])
        instance['image'] = image
        self.instance = instance
        docker_client.start(instance['Id'], binds=self.binds)
        log.msg('Container started')
        return [instance['Id'], self.image]

    def stop_instance(self, fast=False):
        if self.instance is None:
            # be gentle. Something may just be trying to alert us that an
            # instance never attached, and it's because, somehow, we never
            # started.
            return defer.succeed(None)
        instance = self.instance
        self.instance = None
        return threads.deferToThread(self._thd_stop_instance, instance, fast)

    def _thd_stop_instance(self, instance, fast):
        docker_client = client.Client(**self._get_client_params())
        log.msg('Stopping container %s...' % instance['Id'][:6])
        docker_client.stop(instance['Id'])
        if not fast:
            docker_client.wait(instance['Id'])
        docker_client.remove_container(instance['Id'], v=True, force=True)
        if self.image is None:
            try:
                docker_client.remove_image(image=instance['image'])
            except docker.errors.APIError as e:
                log.msg('Error while removing the image: %s', e)
