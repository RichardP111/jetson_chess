#!/bin/bash
# /usr/local/bin/chess-usb-mount.sh
# Called by udev to mount/unmount USB drives at /mnt/usb.
# Uses full binary paths because udevd runs with a minimal PATH.

MOUNT_POINT="/mnt/usb"
LOG="/tmp/chess-usb-mount.log"
ACTION="$1"
DEVICE="$2"

# Full paths — essential when called from udevd
MOUNT_BIN="/bin/mount"
UMOUNT_BIN="/bin/umount"
BLKID_BIN="/sbin/blkid"
MKDIR_BIN="/bin/mkdir"
SYNC_BIN="/bin/sync"
FINDMNT_BIN="/bin/findmnt"
MOUNTPOINT_BIN="/bin/mountpoint"

log() {
    echo "$(date '+%Y-%m-%d %H:%M:%S') [$ACTION] $*" >> "$LOG" 2>/dev/null || true
    # Also log to syslog so it's visible even if file write fails
    logger -t chess-usb "[$ACTION] $*" 2>/dev/null || true
}

# Ensure log file is world-writable so both root (udev) and user (dev) can write
touch "$LOG" 2>/dev/null && chmod 666 "$LOG" 2>/dev/null || true

case "$ACTION" in

  mount)
    if [ -z "$DEVICE" ]; then
        log "ERROR: no device given"
        exit 1
    fi

    # Give kernel 1s to finish settling the device node
    sleep 1

    # Already mounted somewhere?
    EXISTING=$("$FINDMNT_BIN" -n -o TARGET "$DEVICE" 2>/dev/null)
    if [ -n "$EXISTING" ]; then
        log "Device $DEVICE already mounted at $EXISTING — skipping"
        exit 0
    fi

    # If /mnt/usb is already mounted by something else, bail
    if "$MOUNTPOINT_BIN" -q "$MOUNT_POINT"; then
        log "/mnt/usb already in use — skipping"
        exit 0
    fi

    "$MKDIR_BIN" -p "$MOUNT_POINT"

    # Detect filesystem type using full path to blkid
    FS_TYPE=$("$BLKID_BIN" -o value -s TYPE "$DEVICE" 2>/dev/null)
    log "Mounting $DEVICE (fs=${FS_TYPE:-unknown}) at $MOUNT_POINT"

    case "$FS_TYPE" in
      vfat|fat32|fat16)
        "$MOUNT_BIN" -t vfat -o rw,uid=0,gid=0,umask=000,flush "$DEVICE" "$MOUNT_POINT"
        ;;
      exfat)
        # Try kernel driver first, fall back to fuse
        "$MOUNT_BIN" -t exfat -o rw,uid=0,gid=0,umask=000 "$DEVICE" "$MOUNT_POINT" 2>/dev/null \
          || /usr/sbin/mount.exfat-fuse "$DEVICE" "$MOUNT_POINT"
        ;;
      ntfs)
        /usr/bin/ntfs-3g -o rw,uid=0,gid=0,umask=000 "$DEVICE" "$MOUNT_POINT" 2>/dev/null \
          || "$MOUNT_BIN" -t ntfs -o rw "$DEVICE" "$MOUNT_POINT"
        ;;
      ext2|ext3|ext4)
        "$MOUNT_BIN" -t "$FS_TYPE" -o rw "$DEVICE" "$MOUNT_POINT"
        ;;
      "")
        log "ERROR: blkid returned empty filesystem type for $DEVICE — aborting"
        exit 1
        ;;
      *)
        log "Unknown filesystem '$FS_TYPE' — trying auto-detect"
        "$MOUNT_BIN" -o rw "$DEVICE" "$MOUNT_POINT"
        ;;
    esac

    RC=$?
    if [ $RC -eq 0 ]; then
        log "Mount succeeded — $DEVICE at $MOUNT_POINT (fs=$FS_TYPE)"
    else
        log "ERROR: mount returned $RC for $DEVICE"
    fi
    exit $RC
    ;;

  unmount)
    if "$MOUNTPOINT_BIN" -q "$MOUNT_POINT"; then
        "$SYNC_BIN"
        "$UMOUNT_BIN" -l "$MOUNT_POINT"
        log "Unmounted $MOUNT_POINT"
    else
        log "Nothing mounted at $MOUNT_POINT"
    fi
    exit 0
    ;;

  *)
    log "ERROR: unknown action '$ACTION'"
    exit 1
    ;;

esac
# Note: when running chess via 'sudo python3 main.py' or 'systemctl' (User=root),
# the software-mount fallback in usb_drive.py will call this script directly.
# When running as a normal user for development, add a sudoers rule:
#   jetson ALL=(root) NOPASSWD: /usr/local/bin/chess-usb-mount.sh
# OR just plug the drive in before starting chess — udev handles it automatically.