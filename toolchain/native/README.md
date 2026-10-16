These unmodified Apache-2.0 AOSP sources supply the two SDK utilities missing
from the Termux package set. The NDK builds them for each bundled ABI.

- `HprofConv.c`: https://android.googlesource.com/platform/dalvik/+/refs/heads/main/tools/hprof-conv/HprofConv.c
- `etc1tool.cpp`: https://android.googlesource.com/platform/development/+/refs/heads/main/tools/etc1tool/etc1tool.cpp
- `etc1.cpp`: https://android.googlesource.com/platform/frameworks/native/+/refs/heads/main/opengl/libs/ETC1/etc1.cpp
- `ETC1/etc1.h`: https://android.googlesource.com/platform/frameworks/native/+/refs/heads/main/opengl/include/ETC1/etc1.h

Source snapshot: 2026-10-16. Each file retains its original copyright and license
header. The bundled `share/agent-toolchain/native/` contains these sources.

`make_f2fs_casefold` is a launcher for the Android-native `make_f2fs` with
casefold/project quota features and UTF-8 encoding selected, matching the
purpose of AOSP's `CONF_CASEFOLD`/`CONF_PROJID` host configuration.
