# IPA Patcher

IPA에 dylib를 넣고, 앱 이름과 아이콘을 바꿔서 새 IPA로 저장하는 도구예요.
SideStore · AltStore · Sideloadly를 합친 사이드로딩 앱을 만드는 프로젝트의 첫 단계예요.

## 웹 버전 (`index.html`)

**👉 https://psyount.github.io/ipa-patcher/**

- 설치 없이 브라우저에서 바로 사용해요. 모든 처리는 브라우저 안에서만 이뤄지고 파일은 업로드되지 않아요.
- 외부 라이브러리 없이 HTML 파일 하나로 되어 있어요 (ZIP · Mach-O · plist 처리를 직접 구현).
- 필요 환경: Safari 16.4+ / Chrome 103+ / Firefox 113+

### 아이폰 홈 화면에 앱처럼 추가

1. 아이폰 Safari로 위 주소를 열어요.
2. 공유 버튼 → **홈 화면에 추가**를 눌러요. (이름은 "IPA 패처"로 나와요)
3. 홈 화면 아이콘으로 열면 주소창 없이 앱처럼 열리고, 한 번 연 뒤에는 오프라인에서도 열려요.
4. 만든 IPA는 **공유 · SideStore 등에서 열기** 버튼으로 SideStore나 '파일' 앱으로 바로 보낼 수 있어요.

큰 IPA(수백 MB 이상)는 아이폰 메모리가 부족해서 앱이 다시 로드될 수 있어요. 그럴 땐 맥에서 만들어 주세요.

## 파이썬 버전 (`ipa_patcher.py`)

macOS 기본 도구(python3, zip, unzip, sips)만 써요.

```bash
python3 ipa_patcher.py 원본.ipa -o 결과.ipa \
    --dylib tweak.dylib --name "내 앱" --icon icon.png
```

| 옵션 | 설명 |
|---|---|
| `--dylib` | 넣을 dylib (여러 번 쓸 수 있음) |
| `--weak` | dylib를 못 불러와도 앱이 꺼지지 않도록 weak 링크 |
| `--name` | 새 앱 이름 (언어별 `InfoPlist.strings`도 같이 수정) |
| `--icon` | 새 아이콘 PNG (정사각형, 1024×1024 권장) |

## 하는 일

1. **dylib 삽입**: `.app/Frameworks/`에 복사하고, 메인 실행 파일의 Mach-O 헤더 빈 공간에 `LC_LOAD_DYLIB`(`@executable_path/Frameworks/…`)를 추가해요. FAT 실행 파일도 지원해요.
2. **이름 변경**: `Info.plist`의 `CFBundleDisplayName` / `CFBundleName`과 각 언어의 `InfoPlist.strings`를 바꿔요.
3. **아이콘 변경**: 아이폰·아이패드용 4가지 크기 PNG를 만들어 넣고, `CFBundleIconName`을 지워서 iOS가 새 PNG를 쓰게 해요.

## 주의

- **서명은 하지 않아요.** 만든 IPA는 SideStore · AltStore · Sideloadly로 설치할 때 서명돼요.
- App Store 원본(암호화된) IPA에는 dylib를 넣어도 실행되지 않아요. 복호화된 IPA를 쓰세요.
- 탈옥 트윅용 dylib가 CydiaSubstrate를 찾으면, 탈옥 안 된 기기에서는 대체 프레임워크(ElleKit 등)도 같이 넣어야 해요.
- 웹 버전은 4GB 넘는 IPA를 지원하지 않아요.
