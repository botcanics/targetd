U![targetd logo](https://fedorahosted.org/targetd/raw-attachment/wiki/Logo/targetd.png)

Remote configuration of a LIO-based storage appliance
-----------------------------------------------------
targetd turns Linux into a remotely-configurable storage appliance. It
supports an HTTP/jsonrpc-2.0 interface to let a remote administrator
allocate volumes from an LVM volume group, and export those volumes
over iSCSI.  It also has the ability to create remote file systems and export
those file systems via NFS/CIFS (work in progress).

targetd's sister project is [libstoragemanagement](http://sourceforge.net/projects/libstoragemgmt/),
which allows admins to configure storage arrays (including targetd) in an array-neutral manner.

targetd development
-------------------
targetd is licensed under the GPLv3. Contributions are welcome.
 
 * Mailing list: [targetd-devel](https://lists.fedorahosted.org/mailman/listinfo/targetd-devel)
 * Source repo: [GitHub](https://github.com/agrover/targetd)
 * Bugs: [GitHub](https://github.com/agrover/targetd/issues) or [Trac](https://fedorahosted.org/targetd/)
 * Tarballs: [fedorahosted](https://fedorahosted.org/releases/t/a/targetd/)

**NOTE: targetd is STORAGE-RELATED software, and may be used to
  remove volumes and file systems without warning from the resources it is
  configured to use. Please take care in its use.**

Getting Started
---------------
targetd has these Python library dependencies:
* [targetcli] (https://github.com/agrover/targetcli-fb) (must be fb*)
* [python-rtslib](https://github.com/agrover/rtslib-fb) 2.1.fb14+  (must be fb*)
* [python-lvm](https://github.com/agrover/python-lvm) 1.2.2+
* [python-setproctitle](https://github.com/dvarrazzo/py-setproctitle)
* [PyYAML](http://pyyaml.org/)

All of these are available in Fedora Rawhide.

### Configuring targetd

A configuration file may be placed at `/etc/target/targetd.yaml`, and
is in [YAML](http://www.yaml.org/spec/1.2/spec.html) format. Here's
an example:

    user: "foo" # strings quoted, or not
    password: bar
    ssl: false
    target_name: iqn.2003-01.org.example.mach1:1234

    #Note: The uuid is currently not being used and can be set to null,
    #it will eventually be used for more positive storage identification.
    #You can have as many pools as you wish.  Use the form
    #(white space sensitive):
    #<sp><sp><name>:<sp>{type:<sp>[fs|block],<sp>uuid:<sp><uuid>}
    pools:
      vg-targetd: {type: block, uuid: null}
      vg-targetd-too: {type: block, uuid: c7tiaU-YTF3-v1O5-zVFA-55kq-czvK-DCmyUg}
      /mnt/btrfs: {type: fs, uuid: c1ac7716-2a6e-42fe-8cbf-807eeaaf3ebc}

targetd defaults to using the "vg-targetd" volume group, and username 'admin'
password 'targetd' for the HTTP jsonrpc interface.

Then, run `sudo ./targetd.py`.

client.py is a basic testing script, to get started making API calls.
