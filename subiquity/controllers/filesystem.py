# Copyright 2015 Canonical, Ltd.
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as
# published by the Free Software Foundation, either version 3 of the
# License, or (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU Affero General Public License for more details.
#
# You should have received a copy of the GNU Affero General Public License
# along with this program.  If not, see <http://www.gnu.org/licenses/>.

import enum
import json
import logging
import os
import select
import time

import pyudev

from subiquitycore.controller import BaseController
from subiquitycore.utils import run_command

from subiquity.models.filesystem import (
    align_up,
    Bootloader,
    DeviceAction,
    Partition,
    raidlevels_by_value,
    )
from subiquity.ui.views import (
    FilesystemView,
    GuidedDiskSelectionView,
    GuidedFilesystemView,
    )
from subiquity.ui.views.filesystem.probing import (
    SlowProbing,
    ProbingFailed,
    )


log = logging.getLogger("subiquitycore.controller.filesystem")
block_discover_log = logging.getLogger('block-discover')

BIOS_GRUB_SIZE_BYTES = 1 * 1024 * 1024    # 1MiB
PREP_GRUB_SIZE_BYTES = 8 * 1024 * 1024    # 8MiB
UEFI_GRUB_SIZE_BYTES = 512 * 1024 * 1024  # 512MiB EFI partition


class ProbeState(enum.IntEnum):
    NOT_STARTED = enum.auto()
    PROBING = enum.auto()
    FAILED = enum.auto()
    DONE = enum.auto()


class Probe:

    def __init__(self, controller, restricted, timeout, cb):
        self.controller = controller
        self.restricted = restricted
        self.timeout = timeout
        self.cb = cb
        self.state = ProbeState.NOT_STARTED
        self.result = None

    def start(self):
        block_discover_log.debug(
            "starting probe restricted=%s", self.restricted)
        self.state = ProbeState.PROBING
        self.controller.run_in_bg(self._bg_probe, self._probed)
        self.controller.loop.set_alarm_in(self.timeout, self._check_timeout)

    def _bg_probe(self):
        if self.restricted:
            probe_types = {'blockdev'}
        else:
            probe_types = None
        debug_flags = self.controller.debug_flags
        if 'bpfail-full' in debug_flags and not self.restricted:
            time.sleep(2)
            1/0
        if 'bpfail-restricted' in debug_flags and self.restricted:
            time.sleep(2)
            1/0
        # Should consider invoking probert in a subprocess here (so we
        # can kill it if it gets stuck).
        return self.controller.app.prober.get_storage(probe_types=probe_types)

    def _probed(self, fut):
        if self.state == ProbeState.FAILED:
            block_discover_log.debug(
                "ignoring result %s for timed out probe", fut)
            return
        try:
            self.result = fut.result()
        except Exception:
            block_discover_log.exception(
                "probing failed restricted=%s", self.restricted)
            # Should make a crash report here!
            self.state = ProbeState.FAILED
        else:
            block_discover_log.exception(
                "probing successful restricted=%s", self.restricted)
            self.state = ProbeState.DONE
        self.cb(self)

    def _check_timeout(self, loop, ud):
        if self.state != ProbeState.PROBING:
            return
        # Should make a crash report here!
        block_discover_log.exception(
            "probing timed out restricted=%s", self.restricted)
        self.state = ProbeState.FAILED
        self.cb(self)


class FilesystemController(BaseController):

    def __init__(self, app):
        super().__init__(app)
        self.model = app.base_model.filesystem
        if self.opts.dry_run and self.opts.bootloader:
            name = self.opts.bootloader.upper()
            self.model.bootloader = getattr(Bootloader, name)
        self.answers.setdefault('guided', False)
        self.answers.setdefault('guided-index', 0)
        self.answers.setdefault('manual', [])
        self._cur_probe = None
        self._monitor = None
        self._udev_listen_handle = None

    def start(self):
        self._start_probe(restricted=False)
        context = pyudev.Context()
        self._monitor = pyudev.Monitor.from_netlink(context)
        self._monitor.filter_by(subsystem='block')
        self._monitor.enable_receiving()
        self.start_listening_udev()

    def start_listening_udev(self):
        self._udev_listen_handle = self.loop.watch_file(
            self._monitor.fileno(), self._udev_event)

    def stop_listening_udev(self):
        if self._udev_listen_handle is not None:
            self.loop.remove_watch_file(self._udev_listen_handle)
            self._udev_listen_handle = None

    def _udev_event(self):
        cp = run_command(['udevadm', 'settle', '-t', '0'])
        if cp.returncode != 0:
            log.debug("waiting 0.1 to let udev event queue settle")
            self.stop_listening_udev()
            self.loop.set_alarm_in(
                0.1, lambda loop, ud: self.start_listening_udev())
            return
        # Drain the udev events in the queue -- if we stopped listening to
        # allow udev to settle, it's good bet there is more than one event to
        # process and we don't want to kick off a full block probe for each
        # one.  It's a touch unfortunate that pyudev doesn't have a
        # non-blocking read so we resort to select().
        while select.select([self._monitor.fileno()], [], [], 0)[0]:
            action, dev = self._monitor.receive_device()
            log.debug("_udev_event %s %s", action, dev)
        self._start_probe(restricted=False)

    def _start_probe(self, *, restricted):
        self._cur_probe = Probe(self, restricted, 5.0, self._probe_done)
        self._cur_probe.start()

    def _probe_done(self, probe):
        if probe is not self._cur_probe:
            block_discover_log.debug(
                "ignoring result %s for superseded probe", probe.result)
            return
        if probe.state == ProbeState.FAILED:
            if not probe.restricted:
                self._start_probe(restricted=True)
            else:
                if self.showing:
                    self.start_ui()
            return
        if probe.restricted:
            fname = 'probe-data-restricted.json'
        else:
            fname = 'probe-data.json'
        with open(os.path.join(self.app.block_log_dir, fname), 'w') as fp:
            json.dump(probe.result, fp, indent=4)
        try:
            self.model.load_probe_data(probe.result)
        except Exception:
            block_discover_log.exception(
                "load_probe_data failed restricted=%s", probe.restricted)
            # Should make a crash report here!
            if not probe.restricted:
                self._start_probe(restricted=True)
            else:
                # OK, this is a hack
                self._cur_probe.state = ProbeState.FAILED
                if self.showing:
                    self.start_ui()
        else:
            # Should do something here if probing found no devices.
            if self.showing:
                self.start_ui()

    def start_ui(self):
        if self._cur_probe.state == ProbeState.PROBING:
            self.ui.set_body(SlowProbing(self))
        elif self._cur_probe.state == ProbeState.FAILED:
            self.ui.set_body(ProbingFailed(self))
        else:
            # Once we've shown the filesystem UI, we stop listening for udev
            # events as merging system changes with configuration the user has
            # performed would be tricky.  Possibly worth doing though! Just
            # not today.
            self.stop_listening_udev()
            # Should display a message if self._cur_probe.restricted,
            # i.e. full device probing failed.
            self.ui.set_body(GuidedFilesystemView(self))
            if self.answers['guided']:
                self.guided(self.answers.get('guided-method', 'direct'))
            elif self.answers['manual']:
                self.manual()

    def _action_get(self, id):
        dev_spec = id[0].split()
        dev = None
        if dev_spec[0] == "disk":
            if dev_spec[1] == "index":
                dev = self.model.all_disks()[int(dev_spec[2])]
        elif dev_spec[0] == "raid":
            if dev_spec[1] == "name":
                for r in self.model.all_raids():
                    if r.name == dev_spec[2]:
                        dev = r
                        break
        elif dev_spec[0] == "volgroup":
            if dev_spec[1] == "name":
                for r in self.model.all_volgroups():
                    if r.name == dev_spec[2]:
                        dev = r
                        break
        if dev is None:
            raise Exception("could not resolve {}".format(id))
        if len(id) > 1:
            part, index = id[1].split()
            if part == "part":
                return dev.partitions()[int(index)]
        else:
            return dev
        raise Exception("could not resolve {}".format(id))

    def _action_clean_devices_raid(self, devices):
        r = {
            self._action_get(d): v
            for d, v in zip(devices[::2], devices[1::2])
            }
        for d in r:
            assert d.ok_for_raid
        return r

    def _action_clean_devices_vg(self, devices):
        r = {self._action_get(d): 'active' for d in devices}
        for d in r:
            assert d.ok_for_lvm_vg
        return r

    def _action_clean_level(self, level):
        return raidlevels_by_value[level]

    def _answers_action(self, action):
        from subiquitycore.ui.stretchy import StretchyOverlay
        from subiquity.ui.views.filesystem.delete import ConfirmDeleteStretchy
        log.debug("_answers_action %r", action)
        if 'obj' in action:
            obj = self._action_get(action['obj'])
            meth = getattr(
                self.ui.body.avail_list,
                "_{}_{}".format(obj.type, action['action']))
            meth(obj)
            yield
            body = self.ui.body._w
            if not isinstance(body, StretchyOverlay):
                return
            if isinstance(body.stretchy, ConfirmDeleteStretchy):
                if action.get("submit", True):
                    body.stretchy.done()
            else:
                yield from self._enter_form_data(
                    body.stretchy.form,
                    action['data'],
                    action.get("submit", True))
        elif action['action'] == 'create-raid':
            self.ui.body.create_raid()
            yield
            body = self.ui.body._w
            yield from self._enter_form_data(
                body.stretchy.form,
                action['data'],
                action.get("submit", True),
                clean_suffix='raid')
        elif action['action'] == 'create-vg':
            self.ui.body.create_vg()
            yield
            body = self.ui.body._w
            yield from self._enter_form_data(
                body.stretchy.form,
                action['data'],
                action.get("submit", True),
                clean_suffix='vg')
        elif action['action'] == 'done':
            if not self.ui.body.done.enabled:
                raise Exception("answers did not provide complete fs config")
            self.finish()
        else:
            raise Exception("could not process action {}".format(action))

    def manual(self):
        self.ui.set_body(FilesystemView(self.model, self))
        if self.answers['guided']:
            self.finish()
        if self.answers['manual']:
            self._run_iterator(self._run_actions(self.answers['manual']))
            self.answers['manual'] = []

    def guided(self, method):
        v = GuidedDiskSelectionView(self.model, self, method)
        self.ui.set_body(v)
        if self.answers['guided']:
            index = self.answers['guided-index']
            disk = self.model.all_disks()[index]
            v.choose_disk(None, disk)

    def reset(self):
        log.info("Resetting Filesystem model")
        self.model.reset()
        self.manual()

    def cancel(self):
        self.signal.emit_signal('prev-screen')

    def finish(self):
        log.debug("FilesystemController.finish next-screen")
        # start curtin install in background
        self.signal.emit_signal('installprogress:filesystem-config-done')
        # switch to next screen
        self.signal.emit_signal('next-screen')

    def create_mount(self, fs, spec):
        if spec.get('mount') is None:
            return
        mount = self.model.add_mount(fs, spec['mount'])
        if self.model.needs_bootloader_partition():
            vol = fs.volume
            if vol.type == "partition" and vol.device.type == "disk":
                if vol.device._can_be_boot_disk():
                    self.make_boot_disk(vol.device)
        return mount

    def delete_mount(self, mount):
        if mount is None:
            return
        self.model.remove_mount(mount)

    def create_filesystem(self, volume, spec):
        if spec['fstype'] is None:
            fs = volume.original_fs()
            if fs is None:
                return
            self.model.re_add_filesystem(fs)
        else:
            fs = self.model.add_filesystem(volume, spec['fstype'])
        if isinstance(volume, Partition):
            if spec['fstype'] == "swap":
                volume.flag = "swap"
            elif volume.flag == "swap":
                volume.flag = ""
        if spec['fstype'] == "swap":
            self.model.add_mount(fs, "")
        if spec['fstype'] is None and spec['use_swap']:
            self.model.add_mount(fs, "")
        self.create_mount(fs, spec)
        return fs

    def delete_filesystem(self, fs):
        if fs is None:
            return
        self.delete_mount(fs.mount())
        self.model.remove_filesystem(fs)
    delete_format = delete_filesystem

    def create_partition(self, device, spec, flag="", wipe=None):
        part = self.model.add_partition(device, spec["size"], flag, wipe)
        self.create_filesystem(part, spec)
        return part

    def delete_partition(self, part):
        self.clear(part)
        self.model.remove_partition(part)

    def _create_boot_partition(self, disk):
        bootloader = self.model.bootloader
        if bootloader == Bootloader.UEFI:
            part_size = UEFI_GRUB_SIZE_BYTES
            if UEFI_GRUB_SIZE_BYTES*2 >= disk.size:
                part_size = disk.size // 2
            log.debug('_create_boot_partition - adding EFI partition')
            part = self.create_partition(
                disk,
                dict(size=part_size, fstype='fat32', mount='/boot/efi'),
                flag="boot")
        elif bootloader == Bootloader.PREP:
            log.debug('_create_boot_partition - adding PReP partition')
            part = self.create_partition(
                disk,
                dict(size=PREP_GRUB_SIZE_BYTES, fstype=None, mount=None),
                # must be wiped or grub-install will fail
                wipe='zero',
                flag='prep')
            self.model.grub_install_device = part
        elif bootloader == Bootloader.BIOS:
            log.debug('_create_boot_partition - adding bios_grub partition')
            part = self.create_partition(
                disk,
                dict(size=BIOS_GRUB_SIZE_BYTES, fstype=None, mount=None),
                flag='bios_grub')
            self.model.grub_install_device = disk
        return part

    def create_raid(self, spec):
        for d in spec['devices']:
            self.clear(d)
        raid = self.model.add_raid(
            spec['name'],
            spec['level'].value,
            spec['devices'],
            spec['spare_devices'])
        return raid

    def delete_raid(self, raid):
        if raid is None:
            return
        self.clear(raid)
        for p in list(raid.partitions()):
            self.delete_partition(p)
        self.model.remove_raid(raid)

    def create_volgroup(self, spec):
        devices = set()
        key = spec.get('password')
        for device in spec['devices']:
            self.clear(device)
            if key:
                device = self.model.add_dm_crypt(device, key)
            devices.add(device)
        return self.model.add_volgroup(name=spec['name'], devices=devices)
    create_lvm_volgroup = create_volgroup

    def delete_volgroup(self, vg):
        for lv in list(vg.partitions()):
            self.delete_logical_volume(lv)
        for d in vg.devices:
            if d.type == "dm_crypt":
                self.model.remove_dm_crypt(d)
        self.model.remove_volgroup(vg)
    delete_lvm_volgroup = delete_volgroup

    def create_logical_volume(self, vg, spec):
        lv = self.model.add_logical_volume(
            vg=vg,
            name=spec['name'],
            size=spec['size'])
        self.create_filesystem(lv, spec)
        return lv
    create_lvm_partition = create_logical_volume

    def delete_logical_volume(self, lv):
        self.clear(lv)
        self.model.remove_logical_volume(lv)
    delete_lvm_partition = delete_logical_volume

    def delete(self, obj):
        if obj is None:
            return
        getattr(self, 'delete_' + obj.type)(obj)

    def clear(self, obj):
        for subobj in obj.fs(), obj.constructed_device():
            self.delete(subobj)

    def reformat(self, disk):
        if disk.type == "disk":
            disk.preserve = False
            disk.wipe = 'superblock-recursive'
        self.clear(disk)
        for p in list(disk.partitions()):
            self.delete(p)

    def partition_disk_handler(self, disk, partition, spec):
        log.debug('partition_disk_handler: %s %s %s', disk, partition, spec)
        log.debug('disk.freespace: {}'.format(disk.free_for_partitions))

        if partition is not None:
            if 'size' in spec:
                partition.size = align_up(spec['size'])
                if disk.free_for_partitions < 0:
                    raise Exception("partition size too large")
            self.delete_filesystem(partition.fs())
            self.create_filesystem(partition, spec)
            return

        if len(disk.partitions()) == 0:
            if disk.type == "disk":
                disk.preserve = False
                disk.wipe = 'superblock-recursive'

        needs_boot = self.model.needs_bootloader_partition()
        log.debug('model needs a bootloader partition? {}'.format(needs_boot))
        can_be_boot = DeviceAction.MAKE_BOOT in disk.supported_actions
        if needs_boot and len(disk.partitions()) == 0 and can_be_boot:
            part = self._create_boot_partition(disk)

            # adjust downward the partition size (if necessary) to accommodate
            # bios/grub partition
            if spec['size'] > disk.free_for_partitions:
                log.debug(
                    "Adjusting request down: %s - %s = %s",
                    spec['size'], part.size, disk.free_for_partitions)
                spec['size'] = disk.free_for_partitions

        self.create_partition(disk, spec)

        log.info("Successfully added partition")

    def logical_volume_handler(self, vg, lv, spec):
        log.debug('logical_volume_handler: %s %s %s', vg, lv, spec)
        log.debug('vg.freespace: {}'.format(vg.free_for_partitions))

        if lv is not None:
            if 'name' in spec:
                lv.name = spec['name']
            if 'size' in spec:
                lv.size = align_up(spec['size'])
                if vg.free_for_partitions < 0:
                    raise Exception("lv size too large")
            self.delete_filesystem(lv.fs())
            self.create_filesystem(lv, spec)
            return

        self.create_logical_volume(vg, spec)

    def add_format_handler(self, volume, spec):
        log.debug('add_format_handler %s %s', volume, spec)
        self.clear(volume)
        self.create_filesystem(volume, spec)

    def raid_handler(self, existing, spec):
        log.debug("raid_handler %s %s", existing, spec)
        if existing is not None:
            for d in existing.devices | existing.spare_devices:
                d._constructed_device = None
            for d in spec['devices'] | spec['spare_devices']:
                self.clear(d)
                d._constructed_device = existing
            existing.name = spec['name']
            existing.raidlevel = spec['level'].value
            existing.devices = spec['devices']
            existing.spare_devices = spec['spare_devices']
        else:
            self.create_raid(spec)

    def volgroup_handler(self, existing, spec):
        if existing is not None:
            key = spec.get('password')
            for d in existing.devices:
                if d.type == "dm_crypt":
                    self.model.remove_dm_crypt(d)
                    d = d.volume
                d._constructed_device = None
            devices = set()
            for d in spec['devices']:
                self.clear(d)
                if key:
                    d = self.model.add_dm_crypt(d, key)
                d._constructed_device = existing
                devices.add(d)
            existing.name = spec['name']
            existing.devices = devices
        else:
            self.create_volgroup(spec)

    def make_boot_disk(self, new_boot_disk):
        boot_partition = None
        if self.model.bootloader == Bootloader.BIOS:
            install_dev = self.model.grub_install_device
            if install_dev:
                boot_partition = install_dev._potential_boot_partition()
        elif self.model.bootloader == Bootloader.UEFI:
            mount = self.model._mount_for_path("/boot/efi")
            if mount is not None:
                boot_partition = mount.device.volume
        elif self.model.bootloader == Bootloader.PREP:
            boot_partition = self.model.grub_install_device
        if boot_partition is not None:
            if boot_partition.preserve:
                if self.model.bootloader == Bootloader.PREP:
                    boot_partition.wipe = None
                elif self.model.bootloader == Bootloader.UEFI:
                    self.delete_mount(boot_partition.fs().mount())
            else:
                boot_disk = boot_partition.device
                full = boot_disk.free_for_partitions == 0
                self.delete_partition(boot_partition)
                if full:
                    largest_part = max(
                        boot_disk.partitions(), key=lambda p: p.size)
                    largest_part.size += boot_partition.size
                if new_boot_disk.free_for_partitions < boot_partition.size:
                    largest_part = max(
                        new_boot_disk.partitions(), key=lambda p: p.size)
                    largest_part.size -= (
                        boot_partition.size -
                        new_boot_disk.free_for_partitions)
        if new_boot_disk._has_preexisting_partition():
            if self.model.bootloader == Bootloader.BIOS:
                self.model.grub_install_device = new_boot_disk
            elif self.model.bootloader == Bootloader.UEFI:
                part = new_boot_disk._potential_boot_partition()
                if part.fs() is None:
                    self.model.add_filesystem(part, 'fat32')
                self.model.add_mount(part.fs(), '/boot/efi')
            elif self.model.bootloader == Bootloader.PREP:
                part = new_boot_disk._potential_boot_partition()
                part.wipe = 'zero'
                self.model.grub_install_device = part
        else:
            new_boot_disk.preserve = False
            self._create_boot_partition(new_boot_disk)
