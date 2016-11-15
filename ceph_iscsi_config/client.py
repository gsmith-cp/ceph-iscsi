#!/usr/bin/env python

__author__ = 'pcuzner@redhat.com'

import os
import rtslib_fb.root as lio_root

from socket import gethostname
from rtslib_fb.target import NodeACL, TPG
from rtslib_fb.utils import RTSLibError, normalize_wwn

import ceph_iscsi_config.settings as settings

from ceph_iscsi_config.common import Config, ansible_control
from ceph_iscsi_config.utils import get_pool_name


class GWClient(object):
    """
    This class holds a representation of a client connecting to LIO
    """

    seed_metadata = {
                     "auth": {"chap": ''},
                     "luns": {}
                     }


    def __init__(self, logger, client_iqn, image_list, chap):
        """
        Instantiate an instance of an LIO client
        :param client_iqn: iscsi iqn string
        :param image_list: list of rbd images (pool/image) to attach to this client
        :param chap: chap credentials in the format 'user/password'
        :return:
        """

        self.iqn = client_iqn
        self.requested_images = image_list      # images are in comma separated pool.image_name format

        self.chap = chap                        # parameters for auth
        self.mutual = ''
        self.tpgauth = ''
        self.metadata = {}
        self.acl = None
        self.client_luns = {}
        self.tpg = None
        self.tpg_luns = {}
        self.lun_id_list = range(256)           # available LUN ids 0..255
        self.change_count = 0
        self.commit_enabled = True              # enable commit to the config for changes by default
        self.logger = logger
        self.current_config = {}

        try:
            valid_iqn = normalize_wwn(['iqn'], client_iqn)
        except RTSLibError as err:
            self.error = True
            self.error_msg = "Invalid client name for iSCSI - {}".format(err)
        else:
            self.error = False
            self.error_msg = ''



    def setup_luns(self):
        """
        Add the requested LUNs to the node ACL definition. The image list defined for the
        client is compared to the current runtime settings, resulting in new images being
        added, or images removed.
        """

        # first drop the current lunid's used from the candidate list
        # this allows luns to be added/removed, and new id's to occupy free lun-id
        # slots rather than simply tag on the end. In a high churn environment,
        # adding new lun(s) at highest lun +1 could lead to exhausting the
        # 255 lun limit per target
        self.client_luns = self.get_images(self.acl)
        for image_name in self.client_luns:
            lun_id = self.client_luns[image_name]['lun_id']
            self.lun_id_list.remove(lun_id)
            self.logger.debug("(Client.setup_luns) {} has id of {}".format(image_name, lun_id))

        self.tpg_luns = self.get_images(self.tpg)
        current_map = dict(self.client_luns)

        for image in self.requested_images:
            if image in self.client_luns:
                del current_map[image]
                continue
            else:
                rc = self._add_lun(image, self.tpg_luns[image])
                if rc != 0:
                    self.error = True
                    self.error_msg = "{} is missing from the tpg - unable to map".format(image)
                    self.logger.debug("(Client.setup) tpg luns {}".format(self.tpg_luns))
                    self.logger.error("(Client.setup) missing image '{}' from the tpg".format(image))
                    return

        # 'current_map' should be empty, if not the remaining images need to be removed
        # from the client
        if current_map:
            for image in current_map:
                self._del_lun_map(image)
                if self.error:
                    self.logger.error("(Client.setup) unable to delete {} from {}".format(self.iqn,
                                                                                          image))
                    return

    def define_client(self):
        """
        Establish the links for this object to the corresponding ACL and TPG objects from LIO
        :return:
        """

        r = lio_root.RTSRoot()

        # NB. this will check all tpg's for a matching iqn
        for client in r.node_acls:
            if client.node_wwn == self.iqn:
                self.acl = client
                self.tpg = client.parent_tpg
                self.logger.debug("(Client.define_client) - {} already defined".format(self.iqn))
                return

        # at this point the client does not exist, so create it
        # The configuration only has one active tpg, so pick that one for any acl definitions
        for tpg in r.tpgs:
            if tpg.enable:
                self.tpg = tpg

        try:
            self.acl = NodeACL(self.tpg, self.iqn)
            # Try to detect network problems so we can kill connections
            # and cleanup before the initiator has begun recovery and
            # failed over.
            self.acl.set_attribute('dataout_timeout', '20')             # default  3
            self.acl.set_attribute('nopin_response_timeout', '10')      # default 30
            self.acl.set_attribute('nopin_timeout', '10')               # default 15
        except RTSLibError as err:
            self.logger.error("(Client.define_client) FAILED to define {}".format(self.iqn))
            self.logger.debug("(Client.define_client) failure msg {}".format(err))
            self.error = True
            self.error_msg = err
        else:
            self.logger.info("(Client.define_client) {} added successfully".format(self.iqn))
            self.change_count += 1


    def configure_auth(self, auth_type, credentials):
        """
        Attempt to configure authentication for the client
        :return:
        """

        client_username, client_password = credentials.split('/')
        if auth_type == 'chap':
            acl_credentials = "{}/{}".format(self.acl.chap_userid,
                                      self.acl.chap_password)

        # if the credential match... nothing to do
        if credentials == acl_credentials:
            return

        # credentials defined on the ACL don't match parms passed from
        # caller so update the acl definition
        try:
            if auth_type == 'chap':
                self.acl.chap_userid = client_username
                self.acl.chap_password = client_password
                self.metadata['auth']['chap'] = credentials

        except RTSLibError as err:
            self.error = True
            self.error_msg = "Unable to (re)configure {} authentication for {} - ".format(auth_type,
                                                                                          self.iqn,
                                                                                          err)
            self.logger.error("Client.configure_auth) failed to set {} credentials for {}".format(auth_type,
                                                                                                  self.iqn))
        else:
            self.change_count += 1

    def _add_lun(self, image, lun):
        """
        Add a given image to the client ACL
        :param image: rbd image name of the form pool/image (str)
        :param lun: rtslib lun object
        :return:
        """

        rc = 0
        # get the tpg lun to map this client to
        tpg_lun = lun['tpg_lun']

        # lunid allocated from the current config object setting, or if this is
        # a new device from the next free lun id 'position'
        if image in self.metadata['luns'].keys():
            lun_id = self.metadata['luns'][image]['lun_id']
        else:
            lun_id = self.lun_id_list[0]        # pick the lowest available lun ID

        self.logger.debug("(Client._add_lun) Adding {} to {} at id {}".format(image, self.iqn, lun_id))

        try:
            m_lun = self.acl.mapped_lun(lun_id, tpg_lun=tpg_lun)
        except RTSLibError as err:
            self.logger.error("Client.add_lun RTSLibError for lun id {} - {}".format(lun_id, err))
            rc = 12
        else:

            self.client_luns[image] = {"lun_id": lun_id,
                                       "mapped_lun": m_lun,
                                       "tpg_lun": tpg_lun}

            self.metadata['luns'][image] = {"lun_id": lun_id}
            self.lun_id_list.remove(lun_id)
            self.logger.info("(Client.add_lun) added image '{}' to {}".format(image, self.iqn))
            self.change_count += 1

        return rc

    def _del_lun_map(self, image):
        """
        Delete a lun from the client's ACL
        :param image: rbd image name to remove
        :return:
        """

        lun = self.client_luns[image]['mapped_lun']
        try:
            lun.delete()
        except RTSLibError as err:
            self.error = True
            self.error_msg = err
        else:
            self.change_count += 1

            # the lun entry could have been deleted by another host, so before we try and
            # delete - make sure it's in our local copy of the metadata!
            if image in self.metadata['luns']:
                del self.metadata['luns'][image]

    def delete(self):
        """
        Delete the client definition from LIO
        :return:
        """

        try:
            self.acl.delete()
            self.change_count += 1
            self.logger.info("(Client.delete) deleted NodeACL for {}".format(self.iqn))
        except RTSLibError as err:
            self.error = True
            self.error_msg = "RTS NodeACL delete failure"
            self.logger.error("(Client.delete) failed to delete client {} - error: {}".format(self.iqn,
                                                                                         err))

    def exists(self):
        """
        This function determines whether this instances iqn is already defined to LIO
        :return: Boolean
        """

        r = lio_root.RTSRoot()
        client_list = [client.node_wwn for client in r.node_acls]
        return self.iqn in client_list

    def seed_config(self, config):
        """
        function to seed the config object with a new client definition
        """

        config.add_item("clients", self.iqn)
        config.update_item("clients", self.iqn, GWClient.seed_metadata)

        # persist the config update, and leave the connection to the ceph object open
        # since adding just the iqn is only the start of the definition
        config.commit("retain")

    def manage(self, rqst_type, committer=None):
        """
        Manage the allocation or removal of this client
        :param rqst_type is either present (try and create the nodeACL), or absent - delete the nodeACL
        :param committer is the host responsible for any commits to the configuration - this is not
               needed for Ansible management, but is used by the CLI->API->GWClient interaction
        """
        # Build a local object representing the rados configuration object
        config_object = Config(self.logger)
        if config_object.error:
            self.error = True
            self.error_msg = config_object.error_msg
            return

        # use current config to hold a copy of the current rados config object (dict)
        self.current_config = config_object.config

        running_under_ansible = ansible_control()
        self.logger.debug("(GWClient.manage) running under ansible? {}".format(running_under_ansible))

        if running_under_ansible:
            update_host = GWClient.get_update_host(self.current_config)
        else:
            update_host = committer

        self.logger.debug("GWClient.manage) update host to handle any config update is {}".format(update_host))

        if rqst_type == "present":

            ###############################################################################
            # Ensure the client exists in LIO                                             #
            ###############################################################################

            # first look at the request to see if it matches the settings already in the config
            # object - if so this is just a rerun, or a reboot so config object updates are not
            # needed when we change the LIO environment
            if self.iqn in self.current_config['clients'].keys():
                self.metadata = self.current_config['clients'][self.iqn]
                config_image_list = sorted(self.metadata['luns'].keys())

                #
                # Does the request match the current config?
                if self.chap == self.metadata['auth']['chap'] and \
                   config_image_list == sorted(self.requested_images):
                    self.commit_enabled = False
            else:
                # requested iqn is not in the config object
                if running_under_ansible:
                    if update_host == gethostname().split('.')[0]:
                        self.seed_config(config_object)
                else:
                    # not ansible, so just run the command
                    self.seed_config(config_object)

                self.metadata = GWClient.seed_metadata

            self.logger.debug("(manage) config updates to be applied from this host: {}".format(self.commit_enabled))

            self.define_client()
            if self.error:
                # unable to define the client!
                return

            bad_images = self.validate_images()
            if not bad_images:

                self.setup_luns()
                if self.error:
                    return

                if '/' in self.chap:

                    self.configure_auth('chap', self.chap)
                    if self.error:
                        return

                else:
                    self.logger.warning("(main) client '{}' configured without security".format(self.iqn))
            else:
                # request for images to map to this client that haven't been added to LIO yet!
                self.error = True
                self.error_msg = "Non-existent images {} requested for {}".format(bad_images, self.iqn)
                return

            # check the client object's change count, and update the config object if this is the updating host
            if self.change_count > 0:

                if self.commit_enabled:

                    if update_host == gethostname().split('.')[0]:
                        # update the config object with this clients settings
                        self.logger.debug("Updating the config object metadata for '{}'".format(self.iqn))
                        config_object.update_item("clients", self.iqn, self.metadata)

                        # persist the config update
                        config_object.commit()

        else:
            ###############################################################################
            # Remove the requested client from the config object and LIO                  #
            ###############################################################################
            if self.exists():
                self.define_client()          # grab the client and parent tpg objects
                self.delete()                 # deletes from the local LIO instance
                if self.error:
                    return
                else:
                    # remove this client from the config

                    if update_host == gethostname().split('.')[0]:
                        self.logger.debug("Removing {} from the config object".format(self.iqn))
                        config_object.del_item("clients", self.iqn)
                        config_object.commit()

            else:
                # desired state is absent, but the client does not exist in LIO - Nothing to do!
                self.logger.info("(main) client {} removal request, but the client is not "
                            "defined to LIO...skipping".format(self.iqn))

    def validate_images(self):
        """
        Confirm that the images listed are actually allocated to the tpg and can
        therefore be used by a client
        :return: a list of images that are NOT in the tpg ... should be empty!
        """
        bad_images = []
        tpg_lun_list = self.get_images(self.tpg).keys()
        self.logger.debug("tpg images: {}".format(tpg_lun_list))
        self.logger.debug("request images: {}".format(self.requested_images))
        for image in self.requested_images:
            if image not in tpg_lun_list:
                bad_images.append(image)

        return bad_images

    @staticmethod
    def get_update_host(config):
        """
        decide which gateway host should be responsible for any config object updates
        :param config: configuration dict from the rados pool
        :return: a suitable gateway host that is online
        """

        ptr = 0
        potential_hosts = [host_name for host_name in config["gateways"].keys()
                           if isinstance(config["gateways"][host_name], dict)]

        # Assume the 1st element from the list is OK for now
        # TODO check the potential hosts are online/available

        return potential_hosts[ptr]

    def get_images(self, rts_object):
        """
        Funtion to return a dict of luns mapped to either a node ACL or the TPG, based on the passed
        object type
        :param rts_object: rtslib object - either NodeACL or TPG
        :return: dict indexed by image name of LUN object attributes
        """

        luns_mapped = {}

        if isinstance(rts_object, NodeACL):
            # return a dict of images assigned to this client
            for m_lun in rts_object.mapped_luns:

                key = m_lun.tpg_lun.storage_object.name
                luns_mapped[key] = {"lun_id": m_lun.mapped_lun,
                                    "mapped_lun": m_lun,
                                    "tpg_lun": m_lun.tpg_lun}

        elif isinstance(rts_object, TPG):
            # return a dict of *all* images available to this tpg
            for m_lun in rts_object.luns:

                key = m_lun.storage_object.name
                luns_mapped[key] = {"lun_id": m_lun.lun,
                                    "mapped_lun": None,
                                    "tpg_lun": m_lun}
        return luns_mapped
