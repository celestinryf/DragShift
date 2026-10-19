# FFmpeg in DragShift

DragShift bundles FFmpeg 9.0.2 as two separate programs, `ffmpeg` and `ffprobe`, and runs them as
subprocesses. FFmpeg is not covered by DragShift's MIT licence. Every DragShift build ships an
**LGPL v3** FFmpeg: none of them is configured with `--enable-gpl` or `--enable-nonfree`, and none
contains libx264, libx265 or any other GPL or nonfree library. The LGPL v3 adds permissions on top
of the GPL v3, so both `COPYING.LGPLv3` and `COPYING.GPLv3` are included.

- **Windows x64, Linux amd64:** BtbN/FFmpeg-Builds LGPL v3 builds
  (https://github.com/BtbN/FFmpeg-Builds), downloaded with a pinned SHA-256 by
  `packaging/ffmpeg/fetch.py`. Their own licence notice is `LICENSE-ffmpeg-build.txt`.
- **macOS (Apple silicon and Intel):** built by DragShift from source with
  `packaging/ffmpeg/build_mac.py`, one native build per architecture, for macOS 11 and later.
  FFmpeg is configured with:

  ```
  ./configure --enable-version3 --disable-autodetect --enable-videotoolbox --enable-audiotoolbox
      --enable-zlib --enable-bzlib --enable-libmp3lame --enable-libopus --enable-libvorbis
      --enable-libvpx --disable-network --disable-ffplay --disable-doc --disable-debug
      --disable-shared --enable-static --pkg-config-flags=--static
  ```

  H.264 is encoded with Apple's VideoToolbox (`h264_videotoolbox`). These libraries are linked in
  statically, each built from its official release with a pinned SHA-256; their licence texts sit
  beside the programs as `LICENSE-<library>-*.txt`:

  | Library | Version | Licence | Source |
  | --- | --- | --- | --- |
  | LAME (MP3) | 4.0 | LGPL v2 or later | https://downloads.sourceforge.net/project/lame/lame/4.0/lame-4.0.tar.gz |
  | Opus | 1.6.1 | BSD 3-clause | https://downloads.xiph.org/releases/opus/opus-1.6.1.tar.gz |
  | libogg | 1.3.6 | BSD 3-clause | https://downloads.xiph.org/releases/ogg/libogg-1.3.6.tar.xz |
  | libvorbis | 1.3.7 | BSD 3-clause | https://downloads.xiph.org/releases/vorbis/libvorbis-1.3.7.tar.xz |
  | libvpx (VP9) | 1.17.0 | BSD 3-clause + patent grant | https://github.com/webmproject/libvpx/archive/refs/tags/v1.17.0.tar.gz |

  The programs link only to macOS's own libraries and frameworks.

**Corresponding source:** FFmpeg 9.0.2 is https://ffmpeg.org/releases/ffmpeg-9.0.2.tar.xz. The
build scripts and library sources are listed above, and each app's `ffmpeg` folder has a
`FFMPEG-NOTICE.txt` saying exactly how that copy was made. To rebuild the Mac programs yourself
(for example with a changed FFmpeg), run `python packaging/ffmpeg/build_mac.py` from DragShift's
source on a Mac and replace the two files in `DragShift.app/Contents/Frameworks/ffmpeg`.

FFmpeg is a trademark of Fabrice Bellard, originator of the FFmpeg project.

The rest of this file is FFmpeg's own `LICENSE.md` from the n9.0.2 tag.

---

# License

Most files in FFmpeg are under the GNU Lesser General Public License version 2.1
or later (LGPL v2.1+). Read the file `COPYING.LGPLv2.1` for details. Some other
files have MIT/X11/BSD-style licenses. In combination the LGPL v2.1+ applies to
FFmpeg.

Some optional parts of FFmpeg are licensed under the GNU General Public License
version 2 or later (GPL v2+). See the file `COPYING.GPLv2` for details. None of
these parts are used by default, you have to explicitly pass `--enable-gpl` to
configure to activate them. In this case, FFmpeg's license changes to GPL v2+.

Specifically, the GPL parts of FFmpeg are:

- optional x86 optimization in the files
    - `libavcodec/x86/flac_dsp_gpl.asm`
    - `libavcodec/x86/idct_mmx.c`
    - `libavfilter/x86/vf_removegrain.asm`
- the following building and testing tools
    - `compat/solaris/make_sunver.pl`
    - `doc/t2h.pm`
    - `doc/texi2pod.pl`
    - `libswresample/tests/swresample.c`
    - `tests/checkasm/*`
    - `tests/tiny_ssim.c`
- the following filters in libavfilter:
    - `signature_lookup.c`
    - `vf_blackframe.c`
    - `vf_boxblur.c`
    - `vf_colormatrix.c`
    - `vf_cover_rect.c`
    - `vf_cropdetect.c`
    - `vf_delogo.c`
    - `vf_eq.c`
    - `vf_find_rect.c`
    - `vf_fspp.c`
    - `vf_histeq.c`
    - `vf_hqdn3d.c`
    - `vf_kerndeint.c`
    - `vf_lensfun.c` (GPL version 3 or later)
    - `vf_mcdeint.c`
    - `vf_mpdecimate.c`
    - `vf_nnedi.c`
    - `vf_owdenoise.c`
    - `vf_perspective.c`
    - `vf_phase.c`
    - `vf_pp7.c`
    - `vf_pullup.c`
    - `vf_repeatfields.c`
    - `vf_sab.c`
    - `vf_signature.c`
    - `vf_smartblur.c`
    - `vf_spp.c`
    - `vf_stereo3d.c`
    - `vf_super2xsai.c`
    - `vf_tinterlace.c`
    - `vf_uspp.c`
    - `vf_vaguedenoiser.c`
    - `vsrc_mptestsrc.c`

Should you, for whatever reason, prefer to use version 3 of the (L)GPL, then
the configure parameter `--enable-version3` will activate this licensing option
for you. Read the file `COPYING.LGPLv3` or, if you have enabled GPL parts,
`COPYING.GPLv3` to learn the exact legal terms that apply in this case.

There are a handful of files under other licensing terms, namely:

* The files `libavcodec/jfdctfst.c`, `libavcodec/jfdctint_template.c` and
  `libavcodec/jrevdct.c` are taken from libjpeg, see the top of the files for
  licensing details. Specifically note that you must credit the IJG in the
  documentation accompanying your program if you only distribute executables.
  You must also indicate any changes including additions and deletions to
  those three files in the documentation.
* `tests/reference.pnm` is under the expat license.


## External libraries

FFmpeg can be combined with a number of external libraries, which sometimes
affect the licensing of binaries resulting from the combination.

### Compatible libraries

The following libraries are under GPL version 2:
- avisynth
- frei0r
- libcdio
- libdavs2
- librubberband
- libvidstab
- libx264
- libx265
- libxavs
- libxavs2
- libxvid

When combining them with FFmpeg, FFmpeg needs to be licensed as GPL as well by
passing `--enable-gpl` to configure.

The following libraries are under LGPL version 3:
- gmp
- libaribb24
- liblensfun

When combining them with FFmpeg, use the configure option `--enable-version3` to
upgrade FFmpeg to the LGPL v3.

The VMAF, mbedTLS, RK MPI, OpenCORE and VisualOn libraries are under the Apache License
2.0. That license is incompatible with the LGPL v2.1 and the GPL v2, but not with
version 3 of those licenses. So to combine these libraries with FFmpeg, the
license version needs to be upgraded by passing `--enable-version3` to configure.

The smbclient library is under the GPL v3, to combine it with FFmpeg,
the options `--enable-gpl` and `--enable-version3` have to be passed to
configure to upgrade FFmpeg to the GPL v3.

### Incompatible libraries

There are certain libraries you can combine with FFmpeg whose licenses are not
compatible with the GPL and/or the LGPL. If you wish to enable these
libraries, even in circumstances that their license may be incompatible, pass
`--enable-nonfree` to configure. This will cause the resulting binary to be
unredistributable.

The Fraunhofer FDK AAC and OpenSSL libraries are under licenses which are
incompatible with the GPLv2 and v3. To the best of our knowledge, they are
compatible with the LGPL.
