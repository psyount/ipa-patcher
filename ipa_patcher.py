#!/usr/bin/env python3
"""
ipa_patcher.py — IPA 수정 예시 프로그램 (macOS용, 외부 패키지 필요 없음)

할 수 있는 것
  1) dylib 삽입      : .app/Frameworks/ 에 복사 + 메인 실행 파일에 LC_LOAD_DYLIB 추가
  2) 앱 이름 변경    : Info.plist + 각 언어(*.lproj/InfoPlist.strings)의 이름까지
  3) 앱 아이콘 변경  : PNG 한 장을 아이폰/아이패드 크기로 만들어 넣고 Info.plist 수정
  4) 새 IPA로 저장

사용 예
  python3 ipa_patcher.py 원본.ipa -o 결과.ipa \
      --dylib tweak.dylib --name "내 앱" --icon icon.png

  # dylib 여러 개, 약한(weak) 링크로 넣기
  python3 ipa_patcher.py 원본.ipa -o 결과.ipa --dylib a.dylib --dylib b.dylib --weak

주의
  - 이 프로그램은 '서명'은 하지 않는다. 결과 IPA는 SideStore / AltStore / Sideloadly 로
    설치할 때 그 앱들이 다시 서명해 준다.
  - App Store에서 받은 그대로의(암호화된) IPA는 dylib를 넣어도 실행되지 않는다.
    복호화된 IPA를 써야 한다. (프로그램이 감지해서 경고해 줌)
"""

import argparse
import os
import plistlib
import re
import shutil
import struct
import subprocess
import sys
import tempfile
from pathlib import Path

# ──────────────────────────────────────────────────────────────
# 1. Mach-O (iOS 실행 파일 형식) 다루기
# ──────────────────────────────────────────────────────────────
FAT_MAGIC, FAT_MAGIC_64 = 0xCAFEBABE, 0xCAFEBABF   # 여러 CPU용 실행 파일을 묶은 형식
MH_MAGIC, MH_MAGIC_64 = 0xFEEDFACE, 0xFEEDFACF     # 32비트 / 64비트 실행 파일

LC_SEGMENT, LC_SEGMENT_64 = 0x1, 0x19
LC_LOAD_DYLIB, LC_LOAD_WEAK_DYLIB = 0xC, 0x80000018
LC_ENCRYPTION_INFO, LC_ENCRYPTION_INFO_64 = 0x21, 0x2C
DYLIB_CMDS = {0xC, 0xD, 0x80000018, 0x8000001F, 0x80000023}
ZEROFILL_TYPES = {0x1, 0xC, 0x12}                   # 파일에 실제 데이터가 없는 섹션 종류


class PatchError(Exception):
    pass


def macho_slices(data):
    """실행 파일 안의 (시작 위치, 크기) 목록. FAT 파일이면 CPU별 조각이 여러 개."""
    magic = struct.unpack_from(">I", data, 0)[0]
    if magic not in (FAT_MAGIC, FAT_MAGIC_64):
        return [(0, len(data))]
    count = struct.unpack_from(">I", data, 4)[0]
    slices, pos = [], 8
    for _ in range(count):
        if magic == FAT_MAGIC:
            _cpu, _sub, off, size, _align = struct.unpack_from(">iiIII", data, pos)
            pos += 20
        else:
            _cpu, _sub, off, size, _align, _ = struct.unpack_from(">iiQQII", data, pos)
            pos += 32
        slices.append((off, size))
    return slices


def inject_into_slice(data, base, install_path, weak):
    """조각 하나의 load command 목록 끝에 LC_LOAD_DYLIB 를 추가한다.
    반환값: 'added' / 'exists'"""
    magic = struct.unpack_from("<I", data, base)[0]
    if magic == MH_MAGIC_64:
        is64, header_size = True, 32
    elif magic == MH_MAGIC:
        is64, header_size = False, 28
    else:
        raise PatchError(f"Mach-O 실행 파일이 아닙니다 (magic=0x{magic:08x})")

    ncmds, sizeofcmds = struct.unpack_from("<II", data, base + 16)

    # load command 를 하나씩 읽으며 ① 실제 데이터가 시작되는 위치 ② 이미 들어간 dylib ③ 암호화 여부 확인
    first_data = None
    pos = base + header_size
    for _ in range(ncmds):
        cmd, cmdsize = struct.unpack_from("<II", data, pos)

        if cmd in (LC_SEGMENT, LC_SEGMENT_64):
            if cmd == LC_SEGMENT_64:
                nsects = struct.unpack_from("<I", data, pos + 64)[0]
                sect, sect_size, off_at = pos + 72, 80, 48
            else:
                nsects = struct.unpack_from("<I", data, pos + 48)[0]
                sect, sect_size, off_at = pos + 56, 68, 40
            for i in range(nsects):
                s = sect + i * sect_size
                offset = struct.unpack_from("<I", data, s + off_at)[0]
                flags = struct.unpack_from("<I", data, s + off_at + 16)[0]
                if offset and (flags & 0xFF) not in ZEROFILL_TYPES:
                    first_data = offset if first_data is None else min(first_data, offset)

        elif cmd in DYLIB_CMDS:
            name_off = struct.unpack_from("<I", data, pos + 8)[0]
            raw = bytes(data[pos + name_off: pos + cmdsize])
            if raw.split(b"\0", 1)[0].decode(errors="replace") == install_path:
                return "exists"

        elif cmd in (LC_ENCRYPTION_INFO, LC_ENCRYPTION_INFO_64):
            cryptid = struct.unpack_from("<I", data, pos + 16)[0]
            if cryptid != 0:
                raise PatchError("암호화된(App Store 원본) 실행 파일입니다. 복호화된 IPA가 필요합니다.")

        pos += cmdsize

    if first_data is None:
        raise PatchError("섹션 정보를 찾지 못했습니다.")

    # 새 load command 만들기 (dylib_command 구조체 + 경로 문자열)
    name = install_path.encode() + b"\0"
    align = 8 if is64 else 4
    cmdsize = (24 + len(name) + align - 1) // align * align
    new_cmd = struct.pack(
        "<IIIIII",
        LC_LOAD_WEAK_DYLIB if weak else LC_LOAD_DYLIB,
        cmdsize,
        24,        # 경로 문자열이 시작되는 위치
        2,         # timestamp
        0x10000,   # current_version 1.0.0
        0x10000,   # compatibility_version 1.0.0
    ) + name.ljust(cmdsize - 24, b"\0")

    # load command 목록 뒤 ~ 실제 코드 시작 전의 빈 공간(0으로 채워진 곳)에 써 넣는다
    insert_at = base + header_size + sizeofcmds
    if header_size + sizeofcmds + cmdsize > first_data:
        raise PatchError("실행 파일 헤더에 빈 공간이 부족해서 dylib를 넣을 수 없습니다.")
    if any(data[insert_at: insert_at + cmdsize]):
        raise PatchError("넣으려는 자리가 비어 있지 않습니다. (예상과 다른 구조)")

    data[insert_at: insert_at + cmdsize] = new_cmd
    struct.pack_into("<II", data, base + 16, ncmds + 1, sizeofcmds + cmdsize)
    return "added"


def inject_dylib(executable, install_path, weak=False):
    data = bytearray(executable.read_bytes())
    results = [inject_into_slice(data, off, install_path, weak) for off, _ in macho_slices(data)]
    executable.write_bytes(data)   # 같은 파일에 덮어쓰므로 실행 권한은 그대로 유지됨
    return results


# ──────────────────────────────────────────────────────────────
# 2. plist / 이름 바꾸기
# ──────────────────────────────────────────────────────────────
def load_plist(path):
    raw = path.read_bytes()
    fmt = plistlib.FMT_BINARY if raw.startswith(b"bplist") else plistlib.FMT_XML
    return plistlib.loads(raw), fmt


def save_plist(path, obj, fmt):
    path.write_bytes(plistlib.dumps(obj, fmt=fmt))


def patch_localized_names(app, new_name):
    """한국어·영어 등 언어별 InfoPlist.strings 에 이름이 따로 있으면 그것도 바꾼다.
    (이걸 안 바꾸면 Info.plist 를 고쳐도 홈 화면에는 옛 이름이 보일 수 있음)"""
    changed = []
    for strings in app.glob("*.lproj/InfoPlist.strings"):
        raw = strings.read_bytes()

        if raw.startswith(b"bplist") or raw.lstrip().startswith(b"<?xml"):
            obj, fmt = load_plist(strings)
            hit = False
            for key in ("CFBundleDisplayName", "CFBundleName"):
                if key in obj:
                    obj[key], hit = new_name, True
            if hit:
                save_plist(strings, obj, fmt)
                changed.append(strings.parent.name)
            continue

        # 텍스트 형식:  "CFBundleDisplayName" = "이름";
        encoding = "utf-16" if raw[:2] in (b"\xff\xfe", b"\xfe\xff") else "utf-8"
        text = raw.decode(encoding)
        escaped = new_name.replace("\\", "\\\\").replace('"', '\\"')
        pattern = r'("?(?:CFBundleDisplayName|CFBundleName)"?\s*=\s*)"(?:[^"\\]|\\.)*"'
        new_text, n = re.subn(pattern, lambda m: m.group(1) + '"' + escaped + '"', text)
        if n:
            strings.write_bytes(new_text.encode(encoding))
            changed.append(strings.parent.name)
    return changed


# ──────────────────────────────────────────────────────────────
# 3. 아이콘 바꾸기
# ──────────────────────────────────────────────────────────────
ICON_PREFIX = "CustomIcon"
ICON_SIZES = [  # (파일 이름 꼬리, 픽셀)
    ("60x60@2x.png", 120),         # 아이폰
    ("60x60@3x.png", 180),         # 아이폰 (Plus/Pro Max 등)
    ("76x76@2x~ipad.png", 152),    # 아이패드
    ("83.5x83.5@2x~ipad.png", 167),  # 아이패드 Pro/Air
]


def resize_png(src, dst, px):
    if shutil.which("sips"):  # macOS 기본 내장 이미지 도구
        subprocess.run(["sips", "-s", "format", "png", "-z", str(px), str(px), str(src), "--out", str(dst)],
                       check=True, capture_output=True)
        return
    try:
        from PIL import Image  # Mac이 아닐 때 대비
    except ImportError:
        raise PatchError("이미지 크기 변환 도구가 없습니다. (macOS의 sips 또는 pip install pillow)")
    Image.open(src).convert("RGBA").resize((px, px), Image.LANCZOS).save(dst, "PNG")


def set_icon(app, info, icon_src):
    for tail, px in ICON_SIZES:
        resize_png(icon_src, app / f"{ICON_PREFIX}{tail}", px)

    # CFBundleIconName(Assets.car 속 아이콘)을 빼면 iOS가 아래 PNG 목록을 대신 사용한다.
    iphone = info.get("CFBundleIcons", {})
    iphone["CFBundlePrimaryIcon"] = {"CFBundleIconFiles": [f"{ICON_PREFIX}60x60"]}
    info["CFBundleIcons"] = iphone

    ipad = info.get("CFBundleIcons~ipad", {})
    ipad["CFBundlePrimaryIcon"] = {
        "CFBundleIconFiles": [f"{ICON_PREFIX}60x60", f"{ICON_PREFIX}76x76", f"{ICON_PREFIX}83.5x83.5"]
    }
    info["CFBundleIcons~ipad"] = ipad

    info.pop("CFBundleIconFile", None)
    info.pop("CFBundleIconFiles", None)


# ──────────────────────────────────────────────────────────────
# 4. 전체 흐름
# ──────────────────────────────────────────────────────────────
def patch_ipa(ipa, out, dylibs=(), new_name=None, icon=None, weak=False):
    ipa, out = Path(ipa).resolve(), Path(out).resolve()
    work = Path(tempfile.mkdtemp(prefix="ipa_patch_"))
    try:
        print(f"[1/5] 압축 풀기: {ipa.name}")
        subprocess.run(["unzip", "-q", str(ipa), "-d", str(work)], check=True)

        apps = list((work / "Payload").glob("*.app"))
        if len(apps) != 1:
            raise PatchError("Payload 폴더 안에서 .app 을 찾지 못했습니다. 올바른 IPA인가요?")
        app = apps[0]
        info_path = app / "Info.plist"
        info, info_fmt = load_plist(info_path)
        executable = app / info["CFBundleExecutable"]
        print(f"      앱: {app.name}  (실행 파일: {executable.name}, 번들 ID: {info.get('CFBundleIdentifier')})")

        print("[2/5] dylib 삽입")
        if not dylibs:
            print("      (건너뜀)")
        for d in dylibs:
            d = Path(d)
            frameworks = app / "Frameworks"
            frameworks.mkdir(exist_ok=True)
            target = frameworks / d.name
            shutil.copy2(d, target)
            os.chmod(target, 0o755)
            install_path = f"@executable_path/Frameworks/{d.name}"
            results = inject_dylib(executable, install_path, weak)
            print(f"      {d.name} → {install_path}  ({', '.join(results)})")
            if b"CydiaSubstrate" in d.read_bytes():
                print("      ⚠ 이 dylib는 CydiaSubstrate 를 찾습니다. 탈옥 안 된 기기에서는 "
                      "CydiaSubstrate(또는 ElleKit/Substitute) 대체 프레임워크도 같이 넣어야 실행됩니다.")

        print("[3/5] 이름 변경")
        if new_name:
            old = info.get("CFBundleDisplayName") or info.get("CFBundleName")
            info["CFBundleDisplayName"] = new_name
            info["CFBundleName"] = new_name
            langs = patch_localized_names(app, new_name)
            print(f"      '{old}' → '{new_name}'" + (f"  (언어별 파일도 수정: {', '.join(langs)})" if langs else ""))
        else:
            print("      (건너뜀)")

        print("[4/5] 아이콘 변경")
        if icon:
            set_icon(app, info, Path(icon))
            print(f"      {Path(icon).name} → {len(ICON_SIZES)}개 크기로 변환해서 넣음")
        else:
            print("      (건너뜀)")

        save_plist(info_path, info, info_fmt)

        print(f"[5/5] 새 IPA 저장: {out}")
        if out.exists():
            out.unlink()
        # -y: 심볼릭 링크 유지, zip/unzip 은 실행 권한도 유지해 준다
        subprocess.run(["zip", "-qry", str(out), *sorted(os.listdir(work))], cwd=work, check=True)
        print("완료! SideStore / AltStore / Sideloadly 로 설치하면 됩니다.")
    finally:
        shutil.rmtree(work, ignore_errors=True)


def main():
    p = argparse.ArgumentParser(description="IPA에 dylib 삽입 / 이름 변경 / 아이콘 변경")
    p.add_argument("ipa", help="원본 IPA 파일")
    p.add_argument("-o", "--output", help="저장할 IPA (기본: 원본이름_patched.ipa)")
    p.add_argument("--dylib", action="append", default=[], help="넣을 dylib (여러 번 쓸 수 있음)")
    p.add_argument("--weak", action="store_true", help="dylib가 없어도 앱이 죽지 않도록 weak 로 링크")
    p.add_argument("--name", help="새 앱 이름")
    p.add_argument("--icon", help="새 아이콘 PNG (정사각형, 1024x1024 권장)")
    a = p.parse_args()

    for f in [a.ipa, *a.dylib, *([a.icon] if a.icon else [])]:
        if not Path(f).is_file():
            sys.exit(f"파일이 없습니다: {f}")
    out = a.output or str(Path(a.ipa).with_name(Path(a.ipa).stem + "_patched.ipa"))

    try:
        patch_ipa(a.ipa, out, a.dylib, a.name, a.icon, a.weak)
    except PatchError as e:
        sys.exit(f"오류: {e}")


if __name__ == "__main__":
    main()
