These are excerpts for reproducing a real fuzzy-patch collision, not a kernel
implementation or a buildable kernel fixture.

Patch excerpts are stored as arrays of lines in `patches.json`, preserving the
upstream whitespace exactly without embedding unified-diff prefixes as source
indentation in this repository.

- `task_mmu.c`: includes and mapping functions from the GPL-2.0 Linux file at
  aosp-mirror/kernel_common commit `990189f23bcf3bb2166a69c44fb0c115e33364af`
  (Android 14 / 6.1, February 2025 branch).
- `susfs-maps.patch`: the first three task_mmu.c hunks of
  ShirkNeko/susfs4ksu's `gki-android14-6.1` patch at commit
  `5369359dbd10f6dd8cbdd59d1df7b788b29b168c`.
- `hide-maps.patch`: the task_mmu.c hunks from
  ShirkNeko/SukiSU_patch's `69_hide_stuff.patch`, retrieved October 15, 2026.

Applying SUSFS before the hide patch reports success with fuzz=3 but inserts the
hide logic inside the new SRCU redirect path, between a conditional and its body.
The regression must preserve that conditional and keep hide logic outside the
redirect's lock/unlock scope.
