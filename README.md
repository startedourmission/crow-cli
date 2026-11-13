# crow-cli

Crow(`../crow`)에서 **리버스 에이전트**와 **세션 유지(PTY 런타임)** 두 기능만
뽑아, tmux처럼 쓰는 터미널 프로그램으로 다시 만든 것입니다.

- 터미널을 닫아도 셸과 에이전트가 계속 도는 **세션 · 윈도우 · pane**
- 에이전트는 서버에서 돌고 파일 작업은 내 컴퓨터에서 하는 **리버스 에이전트**
- 저장된 **호스트 · SSH · 키** 관리
- 키보드(tmux식 `Ctrl-B`)와 **마우스** 모두로 조작

설계 근거와 Crow 코드와의 대응은 [docs/DESIGN.md](docs/DESIGN.md)에 있습니다.

## 설치

```sh
brew install startedourmission/tap/crow-cli
crow doctor
```

업그레이드는 `brew update && brew upgrade crow-cli`입니다. 이미 attach된
세션은 이전 코드로 돌고 있으니 detach(`Ctrl-B` → `d`) 후 다시 attach하세요.

소스에서 직접 쓰려면 `python3`(표준 라이브러리만 사용)와 `cc`가 있는
macOS 또는 Linux면 됩니다. tmux도, root도 필요 없습니다.

```sh
make build          # build/crow-session (C11 PTY 데몬)
./crow doctor
```

`./crow`를 PATH에 걸어 쓰세요. 데몬 바이너리가 없으면 처음 실행할 때 `cc`로
자동 빌드합니다.

## 빠르게 시작

```sh
crow                 # 마지막 세션에 붙거나, 없으면 main을 만들어 attach
```

- 화면을 나누려면 pane을 **우클릭** → `좌우로 분할`.
- 서버에서 에이전트를 띄우려면 왼쪽 사이드바 `+ 호스트 추가…` → 저장 →
  `리버스 에이전트 실행 →` → `claude`.
- 빠져나오려면 상태바의 `C-b d detach`를 클릭하거나 `Ctrl-B` → `d`.
  세션은 계속 돌고, `crow`로 다시 붙습니다.

## 화면 구성

```
┌ 사이드바 ────┐┌ pane ──────────┐┌ pane ──────────┐
│ 세션         ││                ││                │
│ 호스트·리버스 ││                ││                │
│ SSH 키       ││                ││                │
└─────────────┘└────────────────┘└────────────────┘
 Hosts  Files   세션 | 종류   0:main  1:logs  +        시각 | C-b d detach | C-b ? help
```

- **사이드바**(왼쪽): 세션 목록, 저장된 호스트와 연결 상태(`● 연결됨`,
  `✗ 연결 실패: 인증 오류` 등), SSH 키. 호스트는 30초마다 백그라운드로
  확인합니다. 리버스 시작 같은 작업이 실패하면 사유가 맨 위에 남습니다.
- **pane**: 각 pane은 독립된 셸이나 에이전트입니다. 포커스된 pane의
  테두리만 색이 들어옵니다.
- **상태바**(맨 아래): `Hosts`는 사이드바를 접고 펴고, `Files`는 파일
  탐색기를 엽니다. 윈도우 탭과 `+`(새 윈도우), 오른쪽 `detach` · `help`도
  클릭됩니다. 폭이 좁으면 단축키 힌트를 줄이고 탭을 먼저 보여줍니다.
- **파일 탐색기**: 지금 pane의 폴더에서 열립니다. 폴더는 한 번 클릭,
  파일은 더블클릭으로 열고(`$EDITOR`가 새 pane으로 뜸), 제목줄 `↻`가
  새로고침입니다.

## 세션 · 윈도우 · pane

tmux와 같은 구조입니다. **세션** 안에 **윈도우**(하단 탭)가 있고, 윈도우를
여러 **pane**으로 나눕니다.

```sh
crow new -s work              # 세션 만들고 attach
crow new -d -s work -- htop   # detached로 명령 실행
crow ls                       # 세션 · 윈도우 목록
crow attach -t work
crow new-window -t work -n logs
crow split-window -t work     # pane 나누기
crow capture -t work:logs     # 윈도우의 최근 출력(replay)
crow info -t work             # 윈도우 셸의 현재 디렉터리
crow rename-session -t work new-name
crow kill-pane -t work:0.1
crow kill-window -t work:logs
crow kill-session -t work
```

**동작 방식**: pane마다 `crow-session` 데몬이 실제 PTY와 프로세스를 붙잡고
있습니다. 터미널을 닫거나 crow가 꺼져도 데몬은 살아 있고, 다시 attach하면
**같은 프로세스**에 최근 출력 4MiB를 되감아 붙습니다. attach 직후에도
화면이 비어 있는 pane은 크기를 한 줄 줄였다 되돌려 앱이 다시 그리게
합니다. 명시적으로 닫은 pane은 attach가 몰래 재시작하지 않고, 호스트를
재부팅하면 PTY는 사라집니다. 세션 · 윈도우 · 레이아웃 정보는
`~/.crow-cli/state.json`에 남습니다.

## 조작

### 마우스

| 어디서 | 무엇을 하면 | 동작 |
| --- | --- | --- |
| pane | 클릭 / 휠 | 포커스 / 스크롤백 (마우스를 쓰는 앱이면 그 앱으로 전달) |
| pane | **우클릭** | 분할 · 확대 · 자리 바꾸기 · 다른 윈도우로 옮기기 · 새 윈도우 · 윈도우 이름 · pane 닫기 |
| pane 사이 경계선 | 드래그 | 크기 조절 (저장됨) |
| 사이드바 세션 | 클릭 / **우클릭** | 전환 / 이동 · 이름 바꾸기 · 세션 삭제 |
| 사이드바 호스트 | 클릭 | 호스트 메뉴 (SSH 접속 · 리버스 · 키 확인 · 수정 · 삭제) |
| 하단 윈도우 탭 | 클릭 / **우클릭** | 전환 / 이동 · 이름 바꾸기 · 윈도우 닫기 |
| 상태바 | `+` · `detach` · `help` | 새 윈도우 · detach · 도움말 |

- 리버스 pane을 우클릭하면 메뉴 맨 위에 지금 작업 폴더가 보이고
  `작업 폴더 바꾸기…`도 있습니다.
- `자리 바꾸기…`를 고른 뒤 바꿀 상대 pane을 클릭합니다(`Esc` 취소).
- 마우스를 쓰는 앱에 우클릭을 보내려면 `Shift`+우클릭.
- 지우거나 닫는 동작은 모두 한 번 더 확인합니다.
- 팝업(호스트 추가, 이름 바꾸기, 폴더 변경)은 버튼, 아래 테두리의
  `Enter …`/`Esc 취소` 글자, 바깥 클릭(= 취소)이 모두 클릭으로 동작합니다.
- 리버스·키 패널 항목은 한 번 클릭으로 실행되고, 제목줄 `‹ 뒤로` /
  `✕ 닫기`로 돌아갑니다.
- ssh가 끊긴 pane의 `Enter: 다시 접속 · q: 연결 끊기 · s: 로컬 셸`과
  "Enter를 누르거나 클릭하면 돌아갑니다" 화면도 클릭으로 넘어갑니다.
- pane 안 글자를 드래그로 선택하려면 터미널의 `Option`+드래그를 쓰세요.

### 키보드 (`Ctrl-B` 다음에)

| 키 | 동작 |
| --- | --- |
| `d` | detach (세션은 계속 실행됨) |
| `%` / `"` | 좌우 / 위아래로 분할 |
| `←↑↓→` / `o` | pane 이동 (방향키는 0.5초 안에 연타 가능) |
| `z` | pane 확대 / 복원 |
| `{` / `}` | 앞 / 뒤 pane과 자리 바꾸기 |
| `!` | pane을 새 윈도우로 떼어내기 |
| `x` | pane 닫기 (확인) |
| `c` | 새 윈도우 |
| `n` / `p` / `0-9` | 다음 / 이전 / 번호로 윈도우 선택 |
| `s` | 세션 고르기 (목록 팝업) |
| `,` / `$` | 윈도우 / 세션 이름 바꾸기 |
| `e` | 파일 탐색기 |
| `a` | 호스트 · 리버스 패널 |
| `f` | 리버스 pane 작업 폴더 바꾸기 |
| `h` | 사이드바 키보드 조작 |
| `?` | 도움말 |
| `Ctrl-B` | `Ctrl-B`를 안쪽 프로그램에 그대로 보냄 (crow 안의 tmux: `C-b C-b c`) |

**사이드바**(`Ctrl-B` → `h`): `↑↓`(또는 `j`/`k`)로 고르고 `Enter`로 엽니다.
세션에서는 `e` 이름 바꾸기 · `d` 삭제, 호스트에서는 `a` 추가 · `e` 수정 ·
`d` 삭제 · `s` SSH 접속, `Esc`로 빠져나옵니다.

**파일 탐색기**: `↑↓`/`j`/`k` 이동, `Enter`/`→` 열기, `←`/Backspace 상위,
`r` 새로고침, `q` 닫기.

**한글 입력 중에도** `Ctrl-B` 다음 키는 같은 자리 영문 키로 읽습니다
(`ㅇ` = `d`). 입력기가 글자를 조합 중이면 한 박자 늦을 수 있습니다.
`CROWCLI_IME_SWITCH=1`이면 명령을 기다리는 동안 입력 소스를 ABC로 바꿨다
되돌리지만, Terminal.app 크래시의 유력한 원인이라 기본은 꺼져 있습니다.

## 리버스 에이전트

에이전트 CLI(`claude` / `codex` / `grok`)는 **서버에서 서버의 로그인으로**
돌고, 파일 읽기 · 쓰기와 셸 명령은 전부 **이 컴퓨터의 작업 폴더**에서
실행됩니다.

### 시작하기

1. 사이드바 **호스트 · 리버스**에서 호스트를 클릭합니다. 처음이면
   `+ 호스트 추가…`로 주소 · 포트 · 키를 저장합니다.
2. `리버스 에이전트 실행 →`에서 에이전트를 고릅니다. 대상은 `Tab`이나
   대상 줄 클릭으로 바꿀 수 있습니다(로컬 샌드박스 포함).
3. `reverse-claude` 같은 세션이 만들어지고 바로 전환됩니다. 같은 에이전트를
   다른 호스트 · 폴더로 또 띄우면 `reverse-claude-2`처럼 번호가 붙고,
   같은 호스트 · 폴더 · 에이전트를 다시 고르면 기존 세션으로 들어갑니다.

`Ctrl-B` → `a`로 열면 마지막으로 쓴 호스트가 대상으로 잡힙니다.

### 작업 폴더

- 시작할 때 **포커스된 pane의 셸이 지금 있는 폴더**가 작업 폴더가 됩니다.
  원하는 폴더에서 열려면 그 pane에서 먼저 `cd`하세요.
- 시작한 뒤에 바꾸려면 pane 우클릭 → `작업 폴더 바꾸기…`(또는
  `Ctrl-B` → `f`). 경로를 적거나(상대 경로는 지금 폴더 기준), `Tab`
  자동완성, `↑↓` + `Enter`로 하위 폴더, `Ctrl-W`로 한 단계 위로 갑니다.
- 폴더는 에이전트 지시문과 서버 쪽 저장 위치에 고정되어 있어서, 바꾸면
  그 pane의 에이전트가 **새 대화로** 다시 시작합니다. 새 에이전트가 뜬
  뒤에 이전 것을 끄므로 실패하면 원래 pane이 그대로 남습니다.

### 리버스 세션 안에서

- pane을 나누거나 새 윈도우를 열면 셸 대신 **같은 에이전트가 같은 호스트 ·
  폴더로 하나 더** 뜹니다(새 대화). `crow split-window` /
  `crow new-window -t <리버스 세션>`도 같습니다.
- 탐색기에서 파일을 열면 편집기는 이 컴퓨터의 pane으로 열립니다.
- **클립보드 이미지**: 원격 리버스나 SSH pane에서 `Ctrl-V`를 누르면 Mac
  클립보드의 이미지를 PNG로 서버의 비공개 임시 폴더에 올리고, 그 경로를
  입력란에 붙여 넣습니다(엔터는 치지 않음). 이미지가 없으면 평소의
  `Ctrl-V`가 그대로 갑니다. Terminal.app의 `Cmd-V`는 이미지를 보내지
  않으니 `Ctrl-V`를 쓰세요.
- 서버의 claude가 처음 보는 폴더에서 묻는 "이 폴더를 신뢰하나요?"는
  crow-cli가 자기 전용 폴더에 한해 미리 수락해 둡니다.
- ssh 연결만 끊기면 세션을 지우지 않고 다시 붙습니다. 에이전트는 서버의
  데몬에서 계속 돌고, 파일 접근만 다시 연결될 때까지 멈춥니다.

### CLI로 시작하기

```sh
crow reverse-agent --host mac --provider claude --dir ~/project
crow reverse-agent --host user@server --provider codex --dir ~/project -d
crow reverse-agent --local --provider fake --dir ~/project   # 샌드박스 데모
```

`--host`를 생략하면 저장된 첫 호스트를 씁니다.

### 동작 원리와 서버 요건

- 서버의 `~/.crow-cli/` 아래에 런타임을 복사하고 `cc`로 데몬을 빌드합니다.
- 에이전트에는 `crow_client` MCP 서버(`workspace_info`, `list_directory`,
  `read_file`, `write_file`, `edit_file`, `shell`)만 붙고, 서버 쪽 도구는
  비활성화 + PreToolUse 가드 훅으로 막습니다. 파일 도구는 작업 폴더 밖으로
  나갈 수 없습니다.
- MCP 연결은 `ssh -R` 포워드 위의 브리지로 이어집니다. 끊겨도 프록시가
  세션을 유지하고, 읽기 호출은 다시 연결된 뒤 재시도하며, 이미 보낸 쓰기
  호출은 중복 실행하지 않고 중단으로 보고합니다.
- 서버 요건: SSH 접속, `python3`, `cc`, 그리고 쓸 에이전트 CLI의 설치와
  로그인. `--provider fake`는 MCP 왕복을 검증하는 내장 가짜 에이전트입니다.

## 호스트 · SSH · 키

호스트는 리버스 전용이 아닙니다. 사이드바에서 호스트를 클릭하면 **SSH 접속 ·
리버스 에이전트 · 키·연결 확인 · 공개키 설치 · 수정 · 삭제** 메뉴가
열립니다. `별칭`은 한글도 되고(공백 없이), 비우면 주소에서 정해집니다.

```sh
crow host add mac --address user@192.168.0.10 --port 22 --identity ~/.ssh/id_ed25519
crow hosts                   # 저장된 호스트 목록
crow host edit mac --address user@192.168.0.11 --port 2222 --name mac2
crow host edit mac2 --default-port --default-key   # 포트 · 키 지정 해제
crow host check mac          # 연결 + 키 인증 확인 (어떤 키로 되는지 / 실패 이유)
crow host rm mac2

crow ssh mac                 # ssh 접속을 세션으로 (끊기면 다시 접속 · 끊기 · 로컬 셸)
crow host disconnect mac     # ssh 세션 닫기

crow keys                    # 관리 키 + ~/.ssh 자동 발견, 지문 표시
crow key add work            # Ed25519 키 생성 (~/.ssh/crow_work)
crow key add old --file ~/.ssh/id_rsa   # 기존 키 가져오기
crow key show work           # 지문 + 공개키
crow key copy work           # 공개키를 클립보드에 복사
crow key rename work home
crow key install work mac    # 공개키를 서버 authorized_keys에 설치
crow host edit mac --key work   # 관리 키를 호스트에 연결
crow key rm work             # 호스트가 쓰는 중인 키는 삭제 거부
```

UI에서는 사이드바 **SSH 키** 섹션의 `+ 새 키 만들기…`와 `⋯ 키 목록·관리`로
같은 일을 합니다. 키 이름 · 암호 · 서버 비밀번호처럼 입력이 필요한 단계는
잠깐 일반 터미널로 나왔다가 돌아옵니다. 키 파일은 시스템 도구
(`ssh-keygen` / `ssh-copy-id` / `~/.ssh`)를 그대로 씁니다.

## 테스트

```sh
make test
```

| 파일 | 내용 |
| --- | --- |
| `tests/test_runtime.py` | PTY 데몬: detach/reattach 시 같은 프로세스, 출력 지속, 리사이즈, 종료 코드, 권한 |
| `tests/test_cli.py` | CLI로 attach → detach → reattach, 윈도우 전환, kill |
| `tests/test_panes.py` | 레이아웃 계산, pane 옮기기, VT 화면, pane 합성 |
| `tests/test_ui.py` | 마우스 · 메뉴 · 팝업 · 사이드바 · 상태바 · 탐색기 · 빈 화면 복구 |
| `tests/test_reverse.py` | fake 에이전트가 작업 폴더에만 쓰는지, 가드, 브리지 재연결, 폴더 변경 |
| `tests/test_keys.py` | SSH 키 · 호스트 확인 · ssh 접속 |

## 한계

- 레이아웃 순환, copy-mode 스크롤백 열람, pane 안 글자 드래그 선택은 아직
  없습니다.
- 원격 리버스 윈도우의 `crow capture`는 아직 안 됩니다(attach는 됩니다).
- 실서버 SSH 경로는 로컬 샌드박스(`--local`)와 같은 코드를 쓰지만, 개발
  머신에 sshd가 없어 실서버 E2E는 서버가 있는 환경에서 확인이 필요합니다.
