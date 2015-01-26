# This program is free software; you can redistribute it and/or modify
# it under the terms of the GNU General Public License as
# published by the Free Software Foundation; either version 3 of the
# License, or (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <http://www.gnu.org/licenses/>.
#
# Copyright 2012-2013, Andy Grover <agrover@redhat.com>
#
# Routines to export block devices over iscsi.

import contextlib
from rtslib_fb import (
    Target, TPG, NodeACL, FabricModule, BlockStorageObject, RTSRoot,
    NetworkPortal, LUN, MappedLUN, RTSLibError, RTSLibNotInCFS, NodeACLGroup)
import lvm
from main import TargetdError
from utils import ignored, name_check


def get_vg_lv(pool_name):
    """
    Checks for the existence of a '/' in the pool name.  We are using this
    as an indicator that the vg & lv refer to a thin pool.
    """
    if '/' in pool_name:
        return pool_name.split('/')
    else:
        return pool_name, None


def pool_check(pool_name):
    """
    pool_name *cannot* be trusted, funcs taking a pool param must call
    this or vgopen() to ensure passed-in pool name is one targetd has
    been configured to use.
    """
    pool_to_check = get_vg_lv(pool_name)[0]

    if pool_to_check not in [get_vg_lv(x)[0] for x in pools]:
        raise TargetdError(-110, "Invalid pool")


@contextlib.contextmanager
def vgopen(pool_name):
    """
    Helper function to check/close vg for us.
    """
    global lib_calls
    pool_check(pool_name)
    with contextlib.closing(lvm.vgOpen(pool_name, "w")) as vg:
        yield vg

    # Clean library periodically
    lib_calls += 1
    if lib_calls > 50:
        try:
            # May not be present if using older library
            lvm.gc()
        except AttributeError:
            pass
        lib_calls = 0

pools = []
target_name = None
lib_calls = 0


#
# config_dict must include block_pools and target_name or we blow up
#
def initialize(config_dict):

    global pools
    pools = config_dict['block_pools']

    global target_name
    target_name = config_dict['target_name']

    # fail early if can't access any vg
    for pool in pools:
        vg_name, thin_pool = get_vg_lv(pool)
        test_vg = lvm.vgOpen(vg_name)
        test_vg.close()

        # Allowed multi-pool configs:
        # two thinpools from a single vg: ok
        # two vgs: ok
        # vg and a thinpool from that vg: BAD
        #
        if thin_pool and vg_name in pools:
            raise TargetdError(-1, "VG pool and thin pool from same VG not supported")

    return dict(
        vol_list=volumes,
        vol_create=create,
        vol_destroy=destroy,
        vol_copy=copy,
        export_list=export_list,
        export_create=export_create,
        export_destroy=export_destroy,
        initiator_set_auth=initiator_set_auth,
        initiator_list=initiator_list,
        access_group_list=access_group_list,
        access_group_create=access_group_create,
        access_group_destroy=access_group_destroy,
        access_group_init_add=access_group_init_add,
        access_group_init_del=access_group_init_del,
        access_group_map_list=access_group_map_list,
    )


def volumes(req, pool):
    output = []
    vg_name, lv_pool = get_vg_lv(pool)
    with vgopen(vg_name) as vg:
        for lv in vg.listLVs():
            attrib = lv.getAttr()
            if not lv_pool:
                if attrib[0] == '-':
                    output.append(dict(name=lv.getName(), size=lv.getSize(),
                                       uuid=lv.getUuid()))
            else:
                if attrib[0] == 'V' and lv.getProperty("pool_lv")[0] == lv_pool:
                    output.append(dict(name=lv.getName(), size=lv.getSize(),
                                       uuid=lv.getUuid()))
    return output


def create(req, pool, name, size):
    vg_name, lv_pool = get_vg_lv(pool)
    with vgopen(vg_name) as vg:
        if lv_pool:
            # Fall back to non-thinp if needed
            try:
                vg.createLvThin(lv_pool, name, int(size))
            except AttributeError:
                vg.createLvLinear(name, int(size))
        else:
            vg.createLvLinear(name, int(size))


def destroy(req, pool, name):
    with ignored(RTSLibNotInCFS):
        fm = FabricModule('iscsi')
        t = Target(fm, target_name, mode='lookup')
        tpg = TPG(t, 1, mode='lookup')

        so_name = "%s:%s" % (pool, name)
        if so_name in (lun.storage_object.name for lun in tpg.luns):
            raise TargetdError(-303, "Volume '%s' cannot be "
                                     "removed while exported" % name)

    with vgopen(get_vg_lv(pool)[0]) as vg:
        vg.lvFromName(name).remove()


def copy(req, pool, vol_orig, vol_new, timeout=10):
    """
    Create a new volume that is a copy of an existing one.
    Since 0.6, requires thinp support.
    """
    vg_name, thin_pool = get_vg_lv(pool)

    with vgopen(vg_name) as vg:
        if not thin_pool:
            raise RuntimeError("copy requires thin-provisioned volumes")

        try:
            vg.lvFromName(vol_orig).snapshot(vol_new)
        except AttributeError:
            raise NotImplementedError("liblvm lacks thin snap support")


def export_list(req):
    try:
        fm = FabricModule('iscsi')
        t = Target(fm, target_name, mode='lookup')
        tpg = TPG(t, 1, mode='lookup')
    except RTSLibNotInCFS:
        return []

    exports = []
    for na in tpg.node_acls:
        for mlun in na.mapped_luns:
            mlun_vg, mlun_name = \
                mlun.tpg_lun.storage_object.udev_path.split("/")[2:]
            with vgopen(get_vg_lv(mlun_vg)[0]) as vg:
                lv = vg.lvFromName(mlun_name)
                exports.append(
                    dict(initiator_wwn=na.node_wwn, lun=mlun.mapped_lun,
                         vol_name=mlun_name, pool=mlun_vg,
                         vol_uuid=lv.getUuid(), vol_size=lv.getSize()))
    return exports


def export_create(req, pool, vol, initiator_wwn, lun):
    # get wwn of volume so LIO can export as vpd83 info
    vg_name, thin_pool = get_vg_lv(pool)

    with vgopen(vg_name) as vg:
        vol_serial = vg.lvFromName(vol).getUuid()

    # only add new SO if it doesn't exist
    # so.name concats pool & vol names separated by ':'
    so_name = "%s:%s" % (vg_name, vol)
    try:
        so = BlockStorageObject(so_name)
    except RTSLibError:
        so = BlockStorageObject(so_name, dev="/dev/%s/%s" % (vg_name, vol))
        so.wwn = vol_serial

    # export useful scsi model if kernel > 3.8
    with ignored(RTSLibError):
        so.set_attribute("emulate_model_alias", '1')

    fm = FabricModule('iscsi')
    t = Target(fm, target_name)
    tpg = TPG(t, 1)
    tpg.enable = True
    tpg.set_attribute("authentication", '0')
    NetworkPortal(tpg, "0.0.0.0")
    na = NodeACL(tpg, initiator_wwn)

    # only add tpg lun if it doesn't exist
    for tmp_lun in tpg.luns:
        if tmp_lun.storage_object.name == so.name \
                and tmp_lun.storage_object.plugin == 'block':
            tpg_lun = tmp_lun
            break
    else:
        tpg_lun = LUN(tpg, storage_object=so)

    # only add mapped lun if it doesn't exist
    for tmp_mlun in tpg_lun.mapped_luns:
        if tmp_mlun.mapped_lun == lun:
            break
    else:
        MappedLUN(na, lun, tpg_lun)

    RTSRoot().save_to_file()


def export_destroy(req, pool, vol, initiator_wwn):
    pool_check(pool)
    fm = FabricModule('iscsi')
    t = Target(fm, target_name)
    tpg = TPG(t, 1)
    na = NodeACL(tpg, initiator_wwn)

    vg_name, thin_pool = get_vg_lv(pool)

    for mlun in na.mapped_luns:
        # all SOs are Block so we can access udev_path safely
        mlun_vg, mlun_name = \
            mlun.tpg_lun.storage_object.udev_path.split("/")[2:]

        if mlun_vg == vg_name and mlun_name == vol:
            tpg_lun = mlun.tpg_lun
            mlun.delete()
            # be tidy and delete unused tpg lun mappings?
            if not len(list(tpg_lun.mapped_luns)):
                so = tpg_lun.storage_object
                tpg_lun.delete()
                so.delete()
            break
    else:
        raise TargetdError(-151, "Volume '%s' not found in %s exports" %
                                 (vol, initiator_wwn))

    # Clean up tree if branch has no leaf
    if not len(list(na.mapped_luns)):
        na.delete()
        if not len(list(tpg.node_acls)):
            tpg.delete()
            if not len(list(t.tpgs)):
                t.delete()

    RTSRoot().save_to_file()


def initiator_set_auth(req, initiator_wwn, in_user, in_pass, out_user,
                       out_pass):
    fm = FabricModule('iscsi')
    t = Target(fm, target_name)
    tpg = TPG(t, 1)
    na = NodeACL(tpg, initiator_wwn)

    if not in_user or not in_pass:
        # rtslib treats '' as its NULL value for these
        in_user = in_pass = ''

    if not out_user or not out_pass:
        out_user = out_pass = ''

    na.chap_userid = in_user
    na.chap_password = in_pass

    na.chap_mutual_userid = out_user
    na.chap_mutual_password = out_pass

    RTSRoot().save_to_file()


def block_pools(req):
    results = []

    def thinp_get_free_bytes(thinp):
        # we can only get used percent, so calculate an approx. free bytes
        # These return an integer in of millionths of a percent, so
        # add them and get a decimalization by dividing by another 100
        used_pct = float(thinp.getProperty("data_percent")[0] + \
                         thinp.getProperty("metadata_percent")[0])/100000000
        return int(thinp.getSize() * (1 - used_pct))

    for pool in pools:
        vg_name, tp_name = get_vg_lv(pool)
        if not tp_name:
            with vgopen(vg_name) as vg:
                results.append(dict(name=pool, size=vg.getSize(),
                                    free_size=vg.getFreeSize(), type='block',
                                    uuid=vg.getUuid()))
        else:
            with vgopen(vg_name) as vg:
                thinp = vg.lvFromName(tp_name)
                results.append(dict(name=pool, size=thinp.getSize(),
                                    free_size=thinp_get_free_bytes(thinp),
                                    type='block', uuid=thinp.getUuid()))

    return results


def _get_iscsi_tpg():
    fabric_module = FabricModule('iscsi')
    target = Target(fabric_module, target_name)
    return TPG(target, 1)


def initiator_list(req, standalone_only=False):
    """Return a list of initiator

    Iterate all iSCSI rtslib-fb.NodeACL via rtslib-fb.TPG.node_acls().
    Args:
        req (TargetHandler):  Reserved for future use.
        standalone_only (bool):
            When standalone_only is True, only return initiator which is not
            in any NodeACLGroup (NodeACL.tag is None).
    Returns:
        [
            {
                'init_id':  NodeACL.node_wwn,
                'init_type': 'iscsi',
            },
        ]

        Currently, targetd only support iscsi which means 'init_type' is
        always 'iscsi'.
    Raises:
        N/A
    """
    def _condition(node_acl, standalone_only):
        if standalone_only and node_acl.tag is not None:
            return False
        else:
            return True

    return list(
        {'init_id': node_acl.node_wwn, 'init_type': 'iscsi'}
        for node_acl in _get_iscsi_tpg().node_acls
        if _condition(node_acl, standalone_only))


def access_group_list(req):
    """Return a list of access group

    Iterate all iSCSI rtslib-fb.NodeACLGroup via rtslib-fb.TPG.node_acls().
    Args:
        req (TargetHandler):  Reserved for future use.
    Returns:
        [
            {
                'name':     str,
                'init_ids':  list(str),
                'init_type': 'iscsi',
            },
        ]
        Currently, targetd only support iscsi which means init_type is always
        'iscsi'.
    Raises:
        N/A
    """
    return list(
        {
            'name': node_acl_group.name,
            'init_ids': list(node_acl_group.wwns),
            'init_type': 'iscsi',
        }
        for node_acl_group in _get_iscsi_tpg().node_acl_groups)


def access_group_create(req, ag_name, init_id, init_type):
    if init_type != 'iscsi':
        raise TargetdError(
            TargetdError.NO_SUPPORT, "Only support iscsi")

    name_check(ag_name)

    tpg = _get_iscsi_tpg()

    # Pre-check:
    #   1. Name conflict: requested name is in use
    #   2. Initiator conflict:  request initiator is in use

    for node_acl_group in tpg.node_acl_groups:
        if node_acl_group.name == ag_name:
            raise TargetdError(
                TargetdError.NAME_CONFLICT,
                "Requested access group name is in use")

    if init_id in list(i.node_wwn for i in tpg.node_acls):
        raise TargetdError(
            TargetdError.EXISTS_INITIATOR,
            "Requested init_id is in use")

    node_acl_group = NodeACLGroup(tpg, ag_name)
    node_acl_group.add_acl(init_id)
    RTSRoot().save_to_file()


def access_group_destroy(req, ag_name):
    NodeACLGroup(_get_iscsi_tpg(), ag_name).delete()
    RTSRoot().save_to_file()


def access_group_init_add(req, ag_name, init_id, init_type):
    if init_type != 'iscsi':
        raise TargetdError(
            TargetdError.NO_SUPPORT, "Only support iscsi")

    tpg = _get_iscsi_tpg()
    # Pre-check:
    #   1. Already in requested access group, return silently.
    #   2. Initiator does not exist.
    #   3. Initiator not used by other access group.

    if init_id in list(NodeACLGroup(tpg, ag_name).wwns):
        return

    for node_acl_group in tpg.node_acl_groups:
        if init_id in list(node_acl_group.wwns):
            raise TargetdError(
                TargetdError.EXISTS_INITIATOR,
                "Requested init_id is used by other access group")
    for node_acl in tpg.node_acls:
        if init_id == node_acl.node_wwn:
            raise TargetdError(
                TargetdError.EXISTS_INITIATOR,
                "Requested init_id is in use")

    NodeACLGroup(tpg, ag_name).add_acl(init_id)
    RTSRoot().save_to_file()


def access_group_init_del(req, ag_name, init_id, init_type):
    if init_type != 'iscsi':
        raise TargetdError(
            TargetdError.NO_SUPPORT, "Only support iscsi")

    tpg = _get_iscsi_tpg()

    # Pre-check:
    #   1. Initiator is not in requested access group, return silently.
    if init_id not in list(NodeACLGroup(tpg, ag_name).wwns):
        return

    NodeACLGroup(tpg, ag_name).remove_acl(init_id)
    RTSRoot().save_to_file()


def access_group_map_list(req):
    """
    Return a list of dictionaries in this format:
        {
            'ag_name': ag_name,
            'h_lun_id': h_lun_id,   # host side LUN ID
            'pool_name': pool_name,
            'vol_name': vol_name,
        }
    """
    results = []
    tpg = _get_iscsi_tpg()
    vg_name_2_pool_name_dict = {}
    for pool_name in pools:
        vg_name = get_vg_lv(pool_name)[0]
        vg_name_2_pool_name_dict[vg_name] = pool_name

    for node_acl_group in tpg.node_acl_groups:
        for mapped_lun_group in node_acl_group.mapped_lun_groups:
            tpg_lun = mapped_lun_group.tpg_lun
            so_name = tpg_lun.storage_object.name
            (vg_name, vol_name) = so_name.split(":")
            # When user delete old volume and the created new one with
            # idential name. The mapping status will be kept.
            # Hence we don't expose volume UUID here.
            results.append(
                {
                    'ag_name': node_acl_group.name,
                    'h_lun_id': mapped_lun_group.mapped_lun,
                    'pool_name': vg_name_2_pool_name_dict[vg_name],
                    'vol_name': vol_name,
                }
            )

    return results
