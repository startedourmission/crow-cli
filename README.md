# crow-cli

Crow(`../crow`)에서 **리버스 에이전트**와 **지금은 사라진 세션 유지(PTY 런타임)**
두 기능만 뽑아, tmux/herdr처럼 쓰는 터미널 프로그램으로 다시 만든 것입니다.
설계 근거와 Crow 코드와의 대응은 [docs/DESIGN.md](docs/DESIGN.md)에 있습니다.

## 설치

Homebrew:

```sh
brew install startedourmission/tap/crow-cli
crow doctor
```

소스에서 직접 — 필요한 것: `python3` (표준 라이브러리만 사용), `cc`, macOS 또는 Linux.
tmux도, Homebrew도, root도 필요 없습니다 (Crow의 세션 런타임과 같은 원칙).

```sh
make build          # build/crow-session (C11 PTY 데몬)
./crow doctor
```

`./crow`를 PATH에 걸어 쓰세요. 데몬 바이너리가 없으면 처음 실행할 때
`cc`로 자동 빌드합니다.

## 세션 유지 (tmux처럼)

```sh
crow                        # 마지막 세션에 붙거나, 없으면 main 생성
crow new -s work            # 세션 만들고 attach
crow new -d -s work -- htop # detached로 명령 실행
crow ls                     # 세션/윈도우 목록
crow attach -t work
crow new-window -t work -n logs
crow capture -t work:logs   # 윈도우의 최근 출력(replay) 출력
crow info -t work           # 윈도우 셸의 현재 디렉터리
crow kill-window -t work:logs
crow kill-session -t work
```

마우스가 항상 켜져 있습니다: pane을 클릭하면 포커스, 휠은 그 pane의
스크롤백, 풀스크린 앱(claude 등)이 마우스를 쓰면 그쪽으로 전달됩니다.
상태바 맨 왼쪽 **[Hosts]** 버튼은 왼쪽 탭(세션 · 호스트)을 접었다 폈다 하고
(접으면 pane이 전체 폭을 씀), 그 옆 **[Files]** 버튼을 클릭하면 파일 탐색기가 열립니다
(`Ctrl-B`→`e`도 동일). 탐색기에서는 방향키/`j`/`k` 이동, Enter 열기,
`..`/Backspace 상위, `q` 닫기. 파일을 열면 `$EDITOR`로 새 pane이
분할됩니다.

attach 중에는 화면 **맨 아래 줄에 상태바**가 항상 표시됩니다
(`Hosts Files | 세션 | 종류/에이전트`, 바로 뒤에 **윈도우 탭**, 오른쪽 끝 `시각 | 단축키 힌트`).
윈도우 탭은 현재 윈도우가 하이라이트되고, 클릭하면 바로 그 윈도우로 넘어갑니다.
실제 터미널보다 PTY 높이를 1줄 짧게 알려줘서 셀이나 에이전트 TUI가
상태바를 덮지 않습니다. detach하면 상태바는 사라집니다.

**왼쪽 사이드바**도 attach 내내 표시됩니다: 세션 목록과 저장된 호스트,
각각의 연결 상태(`● 실행 중`/`연결됨`/`연결 실패`/`재접속 대기`)를 보여줍니다.
호스트는 백그라운드로 `ssh … true` 프로브해 30초마다 갱신하므로 attach가
끊기지 않습니다. 실패하면 이유(`인증 오류`/`시간 초과`/`주소 확인 불가`/
`연결 거부` 등)와 함께 **빨간 배경에 ✗** 로 표시되고, 리버스 시작이
실패해도 트레이스백 대신 사이드바 상단에 실패 사유가 남아 다음에 성공할
때까지 계속 보입니다. 세션을 클릭하면 그 세션으로 전환되고, 호스트를 클릭하면
그 호스트로 리버스 패널이 열립니다. 리버스 패널이 떠도 사이드바는
계속 보이지만, 파일 탐색기가 열리면 사이드바는 숨고 탐색기만
보입니다 (pane도 전체 폭을 씁니다).

attach 중에는 tmux와 같은 **Ctrl-B prefix**를 씁니다.

| 키 | 동작 |
| --- | --- |
| `C-b d` | detach (세션은 계속 실행됨) |
| `C-b c` | 새 윈도우 |
| `C-b n` / `C-b p` | 다음/이전 윈도우 |
| `C-b 0-9` | 윈도우 선택 |
| `C-b s` | 다른 세션 선택 |
| `C-b ,` / `C-b $` | 윈도우/세션 이름 변경 |
| `C-b x` | 윈도우 종료 (확인 후) |
| `C-b ?` | 도움말 |

한글 입력 상태에서도 `C-b` 다음 키는 같은 자리 영문 키로 읽습니다(`ㅇ`=`d`).
다만 한글 입력기가 글자를 조합 중이면 다음 키를 칠 때까지 터미널로 넘기지
않아서 한 박자 늦을 수 있습니다. `CROWCLI_IME_SWITCH=1`이면 명령을 기다리는
동안 입력 소스를 ABC로 바꿨다가 되돌리지만, Terminal.app 크래시의 유력한
원인이라 기본은 꺼져 있습니다.

동작 방식: 윈도우마다 `crow-session` 데몬이 실제 PTY와 셸/에이전트 프로세스를
붙잡고 있습니다. 클라이언트가 끊겨도(터미널을 닫아도, Crow를 종료해도) 데몬은
살아 있고, 다시 attach하면 **같은 프로세스**에 4MiB replay를 되감아 붙습니다.
명시적으로 kill한 세션과 종료된 세션은 attach가 몰래 재시작하지 않습니다.
호스트를 재부팅하면 PTY는 사라집니다 (Crow와 동일). 세션/윈도우 메타데이터는
`~/.crow-cli/state.json`에 남습니다.

## 리버스 에이전트 (Crow의 reverse agent)

**UI에서 시작하는 게 기본입니다.** 호스트와 리버스는 왼쪽 탭 하나로
합쳐져 있습니다: 사이드바의 **호스트 · 리버스**에서 호스트를 클릭하면
곧바로 에이전트 선택 패널이 열리고, `+ 호스트 추가…`도 같은 탭에
있습니다. `Ctrl-B`→`a`로 열면 마지막으로 쓴 호스트가 대상으로 잡히며,
패널 안에서 `Tab`으로 대상을 바꿀 수 있습니다:

1. 대상 확인 (왼쪽 탭에서 호스트 클릭, 또는 패널에서 `Tab`)
2. 에이전트 선택 (`claude` / `codex` / `grok`)
3. 시작하면 `agent-<provider>` 세션이 만들어지고 바로 전환됩니다

작업 폴더는 지금 pane의 현재 디렉터리가 클라이언트 워크스페이스가
됩니다. 호스트 추가를 고르면 화면 위에 입력 팝업이 떠서 주소·포트·키(관리 키와
`~/.ssh` 키 중 `←→`로 선택)를 받고, 저장하면 그 호스트 메뉴가 바로 열립니다.
키보드만으로도 됩니다: `Ctrl-B`→`h`를 누르면 왼쪽 사이드바에 포커스가 가서
`↑↓`(또는 `j`/`k`)로 세션·호스트·키 항목을 고르고 `Enter`로 엽니다. 호스트에서
`a` 추가 · `e` 수정 · `d` 삭제 · `s` SSH 접속, `Esc`로 빠져나옵니다.
`별칭` 칸에 원하는 이름(한글 가능, 공백 없이)을 적을 수 있고, 비우면 주소에서
자동으로 정해집니다 (IP면 IP 그대로).
호스트 메뉴의 `호스트 수정…`은 같은 팝업을 현재 값으로 채워 열고(별칭도 변경 가능),
`호스트 삭제…`는 한 번 더 확인한 뒤 지웁니다. 이미 열린 세션은 영향이 없습니다.
이미 있는 리버스 세션은 패널의 "세션 열기"에서 바로 들어갑니다.
세션 이름은 `reverse-<에이전트>`(`reverse-claude`)이고, 같은 에이전트를
다른 호스트·폴더로 또 띄우면 `reverse-claude-2`처럼 번호가 붙습니다.
같은 호스트·폴더·에이전트를 다시 고르면 새로 띄우지 않고 기존 세션으로
들어갑니다.
팝업은 `저장`/`취소` 버튼, 아래 테두리의 `Enter 저장`/`Esc 취소` 글자,
팝업 바깥 클릭(= 취소) 모두 클릭으로 동작합니다.

서버의 claude가 처음 보는 작업 폴더에서 띄우는 "이 폴더를 신뢰하나요?"
질문은 crow-cli가 자기 전용 폴더에 한해 미리 수락해 둡니다 (기본 선택이
"No, exit"라 Enter 한 번에 에이전트가 꺼지던 문제). ssh 연결만 끊긴
경우에는 세션을 지우지 않고 재접속을 시도합니다.

**클립보드 이미지 붙여넣기 (Crow의 이미지 첨부)**: 원격 리버스 에이전트나
SSH 세션 pane에서 `Ctrl-V`를 누르면 Mac 클립보드의 이미지를 PNG로 꺼내
서버의 비공개 임시 폴더(`$TMPDIR/crow-clipboard-…/image.png`, 0700/0600)에
올리고, 그 경로를 입력란에 붙여 넣습니다 (엔터는 치지 않음). 진행 상황은
하단 바에 뜹니다. 클립보드에 이미지가 없으면 평소의 `Ctrl-V`가 그대로
전달되고, 로컬 pane은 claude/codex가 직접 Mac 클립보드를 읽으므로 손대지
않습니다. Terminal.app의 `Cmd-V`는 이미지를 터미널로 보내지 않으니
`Ctrl-V`를 쓰세요.

호스트 관리만 CLI로도 할 수 있습니다:

```sh
./crow host add mac --address user@192.168.0.10 --port 22 --identity ~/.ssh/id_ed25519
./crow hosts          # 저장된 호스트 목록
./crow host edit mac --address user@192.168.0.11 --port 2222 --key work --name mac2
./crow host edit mac2 --default-port --default-key   # 포트·키 지정 해제
./crow host rm mac2
./crow reverse-agent --host mac --provider claude --dir ~/project   # CLI로 바로 시작
```

## SSH 접속 · 키 관리 (Crow의 SSH Keys / SSH)

호스트는 리버스 전용이 아닙니다. 왼쪽 탭에서 호스트를 클릭하면 메뉴가
열립니다: **SSH 접속 / 리버스 에이전트 / 키·연결 확인 / 공개키 설치**.
CLI도 같습니다:

```sh
crow ssh mac                 # ssh 접속을 세션으로 열기 (끊겨도 pane 유지)
crow host check mac          # 연결 + 키 인증 확인 (어떤 키로 되는지/실패 이유)
crow keys                    # 키 목록 (관리 키 + ~/.ssh 자동 발견, 지문 표시)
crow key add work            # Ed25519 키 생성 (~/.ssh/crow_work)
crow key add old --file ~/.ssh/id_rsa   # 기존 키 가져오기
crow key show work           # 지문 + 공개키 (복사용)
crow key copy work           # 공개키를 클립보드에 복사
crow key rename work home    # 관리 키 이름 변경
crow key install work mac    # 공개키를 서버 authorized_keys에 설치
crow key rm work             # 호스트가 쓰는 중인 키는 삭제 거부
crow host add mac --address user@host --key work   # 키 이름으로 호스트에 연결
```

UI에서는 사이드바 **SSH 키** 섹션의 `+ 새 키 만들기…`로 바로 키를 만들고,
`⋯ 키 목록·관리`(또는 키 이름)로 오른쪽 패널을 엽니다. 패널은 **관리 키**와
**이 컴퓨터 · ~/.ssh** 키를 나눠 보여주고, 키별로 **공개키 복사 / 서버에
설치(호스트 선택 → 그 호스트의 키로 지정) / 이름 변경 / 삭제(등록만 해제
또는 파일까지)**를 할 수 있습니다. 이름·암호·
서버 비밀번호처럼 입력이 필요한 단계는 잠깐 일반 터미널로 나왔다가
Enter로 돌아옵니다.

키는 Crow처럼 라이브러리로 관리하되 저장소는 시스템 도구
(`ssh-keygen`/`ssh-copy-id`/`~/.ssh`)를 그대로 씁니다.

`--host`를 생략하면 저장된 첫 호스트를 사용합니다. 기존처럼
`--host user@server`로 직접 적어도 됩니다.

에이전트 CLI는 **서버에서 서버의 로그인으로** 실행되지만, 파일 읽기/쓰기와
셸 명령은 전부 **이 클라이언트의 워크스페이스**에서 실행됩니다.

```sh
crow reverse-agent --host user@server --provider claude --dir ~/project
crow reverse-agent --host user@server --provider codex  --dir ~/project -d
crow reverse-agent --local --provider fake --dir ~/project   # 샌드박스 데모/테스트
```

- 서버에는 `~/.crow-cli/` 아래에 런타임을 복사하고 `cc`로 데몬을 빌드합니다.
- 에이전트에는 `crow_client` MCP 서버(`workspace_info`, `list_directory`,
  `read_file`, `write_file`, `edit_file`, `shell`)만 붙고, 서버 쪽 도구는
  비활성화 + PreToolUse 가드 훅으로 차단됩니다. 도구는 클라이언트
  워크스페이스 밖으로 나갈 수 없습니다.
- MCP 연결은 `ssh -R` 포워드 위의 브리지로 이어지며, 연결이 끊겨도
  프록시가 세션을 유지합니다. 읽기 호출은 재연결 후 재생되고, 이미 보낸
  쓰기 호출은 중복 실행하지 않고 중단으로 보고합니다 (Crow와 동일).
- 에이전트의 PTY도 서버의 `crow-session` 데몬이 유지하므로 SSH가 끊겨도
  에이전트는 계속 돕니다. 클라이언트 파일 접근만 브리지가 다시 붙을 때까지
  멈춥니다.
- 서버 요건: SSH 접속, `python3`, `cc`, 그리고 해당 에이전트 CLI
  (`claude`/`codex`/`grok`) 설치·로그인. `--provider fake`는 MCP 왕복을
  검증하는 내장 가짜 에이전트입니다.

## 테스트

```sh
make test
```

- `tests/test_runtime.py` — Crow에서 삭제된 런타임 테스트를 이식:
  detach/reattach 시 동일 프로세스, detached 중 출력 지속, 리사이즈,
  현재 디렉터리, 명시적 종료 후 재시작 금지, 잘못된 ID/권한 거부, 종료 코드.
- `tests/test_cli.py` — CLI로 attach→`C-b d`→reattach 시 같은 PID인지,
  prefix 윈도우 전환.
- `tests/test_reverse.py` — 샌드박스 "서버"의 fake 에이전트가 클라이언트
  워크스페이스에만 파일을 쓰는지, 가드/샌드박스, 브리지 재시작 후
  읽기 재생(proxy reconnect).

## 한계 (v1)

- 윈도우 안의 pane 분할은 동시 렌더링됩니다 (pane별 VT 화면을 합성).
  다만 pane 비율 조절 드래그, 레이아웃 순환, copy-mode 스크롤백 열람은
  아직 없습니다. 마우스 클릭 포커스와 풀스크린 앱의 마우스 전달은 됩니다.
- 원격(reverse) 윈도우의 `capture`는 아직 로컬 전용입니다. attach는 됩니다.
- SSH 전송 경로는 로컬 샌드박스(`--local`)와 같은 코드(prepare/proxy/
  bridge/데몬)를 쓰지만, 이 개발 머신에는 sshd가 없어 실서버 E2E는
  서버가 있는 환경에서 확인이 필요합니다.
