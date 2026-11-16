# roj – tutorial od A do Z (Windows → GitHub → VPS z Podmanem → pierwsza misja)

Ten dokument prowadzi od rozpakowanego zipa do sytuacji, w której roj działa na serwerze,
sam się aktualizuje po każdym scaleniu w `main`, a pierwsza misja „roj ulepsza roj” przechodzi
pełny etap. Po każdym kroku jest **sprawdzenie** z oczekiwanym wynikiem.

Wzór wdrożenia jest ten sam co w api-gate: GitHub buduje obraz, serwer co 2 minuty sam go
pobiera. Żadnych kluczy SSH ani sekretów wdrożeniowych w GitHubie.

> **Uczciwie o weryfikacji.** Kod i cały łańcuch (misja, Lab, samodoskonalenie, Telegram,
> kopie, eksport) przetestowałem automatycznie offline: 119 testów oraz `roj selftest`.
> **Nie mogłem** zbudować obrazu, uruchomić Podmana, Quadleta ani połączyć się z prawdziwymi
> modelami. Dlatego przy krokach oznaczonych **→ wklej** wklejaj mi wynik; poprawimy różnice.

## Spis

0. Co będzie działać na końcu
1. Czego potrzebujesz
2. [WINDOWS] Kod do GitHuba
3. [GITHUB] CI, obraz, pakiet publiczny
4. [ADMIN] Konto `roj` na VPS i limity
5. [roj] Kontener: pliki, sekrety, pierwszy start
6. [roj] Konfiguracja `roj.toml`: bramka, modele, ceny
7. [roj] Telegram (opcjonalnie)
8. [roj] Automatyczna aktualizacja co 2 minuty
9. [WINDOWS] Panel przez tunel SSH
10. [roj] Kopie zapasowe
11. Pierwsze prawdziwe wywołanie modelu
12. Lab: karty modeli i Playbook
13. Pierwsza misja: „roj ulepsza roj”
14. Samodoskonalenie i pełna pętla: PR → scalenie → CI → serwer
15. Odporność: restart, budżet, awaria modelu
16. Praca codzienna: harmonogramy i flagi
17. Logi dla wspólnego przeglądu
18. Rozwiązywanie problemów
19. Lista kontrolna

Oznaczenia: **[WINDOWS]** Twój komputer (PowerShell), **[GITHUB]** strona github.com,
**[ADMIN]** VPS jako użytkownik z `sudo`, **[roj]** VPS jako użytkownik `roj`.
`<login>` = Twój login GitHub **małymi literami**.

---

## 0. Co będzie działać na końcu

```
[WINDOWS] git push ──► [GITHUB] Actions: testy (Linux) ──► obraz ──► selftest w obrazie ──► ghcr.io/<login>/roj:latest
                                                                                                   │ co ~2 min
[VPS] użytkownik roj (1 CPU / 2 GB)                                                                ▼
   ~/roj.env            sekrety (600)                               podman auto-update ──► kontener "roj"
   ~/data               ROJ_HOME: roj.toml, projekty, bazy, logi, raporty      roj serve: harmonogram, Telegram,
   ~/data/repo          klon roj dla samodoskonalenia                          panel 127.0.0.1:18732, kopie, puls
                         │
                         └── bramka api-gate: http://127.0.0.1:18731/v1 (ta sama maszyna)
```

Ty rozmawiasz z rojem przez CLI na serwerze (`roj ...`), Telegram (zatwierdzenia przyciskami)
i panel web (tunel SSH). Kod roju zmienia się tylko przez pull request, który scalasz Ty.

---

## 1. Czego potrzebujesz

| Co | Skąd | Obowiązkowe |
|---|---|---|
| Konto GitHub, `git` na Windows | git-scm.com | tak |
| VPS z działającą api-gate (Podman rootless, Quadlet, `machinectl`) | Twój tutorial api-gate | tak |
| Klucz bramki dla roj (`sk-gate-…`) | panel api-gate: utwórz **osobny** klucz o nazwie `roj` | tak |
| Lista modeli i ich cen w bramce | `apigate models` (sekcja 6) | tak |
| Bot Telegram | @BotFather | nie (bez niego zatwierdzasz w CLI/panelu) |
| Token GitHub do pull requestów | sekcja 14 | nie (bez niego roj zostawia gałąź i plik patch) |

Podman ≥ 4.9 jest już na serwerze (api-gate używa `Notify=healthy`, roj też).
PostgreSQL i bucket nie są potrzebne do startu (bucket tylko na kopie, sekcja 10).

---

## 2. [WINDOWS] Kod do GitHuba

1. Na GitHubie utwórz **puste** repozytorium `roj` (bez README, bez licencji: są w zipie).
   Publiczne, tak jak api-gate (serwer pobiera obraz bez logowania, a selfdev klonuje bez tokenu).
2. Rozpakuj `roj-v0.4.x.zip` np. do `D:\la2-idea\agent-system\roj` (w zipie jest folder `roj`).

```powershell
cd D:\la2-idea\agent-system\roj
git init -b main
git add -A
git status                       # sprawdź: żadnych plików .env, kluczy, roj.toml z danymi
git commit -m "roj v0.4"
git remote add origin https://github.com/<login>/roj.git
git push -u origin main
```

**Sprawdzenie:** strona repozytorium pokazuje foldery `roj`, `docs`, `deploy`, `tests`
i plik `Dockerfile`.

---

## 3. [GITHUB] CI, obraz, pakiet publiczny

Zakładka **Actions** → przebieg `ci`. Zadania:

| zadanie | co robi | blokuje obraz |
|---|---|---|
| `test (ubuntu-latest, 3.11/3.12/3.13)` | blokada zależności, kontrola łańcucha dostaw, 119 testów | **tak** |
| `test (windows-latest / macos-latest, …)` | to samo na innych systemach | nie (tylko informacja; nigdy tego nie sprawdzałem) |
| `image` | buduje obraz, uruchamia go, `roj healthcheck`, `roj doctor`, **`roj selftest`**, wysyła do `ghcr.io` | – |

**Sprawdzenie:**
- zadania Linux zielone; w logu zadania `image`, krok *Smoke test the image*, jest linia
  `roj selftest: 9/9 passed (offline, stand-in models, no tokens)` → wklej, jeśli coś jest czerwone
  (cały log nieudanego kroku);
- czerwone Windows/macOS nie są problemem dla serwera; wklej mi je osobno, naprawimy później.

**Pakiet publiczny (raz, po pierwszym zielonym przebiegu):** Twój profil → *Packages* → `roj`
→ *Package settings* → *Change visibility* → **Public**.

**Zalecane:** *Settings → Branches → Add rule* dla `main`: *Require a pull request before
merging* i *Require status checks* (`test (ubuntu-latest, 3.12)`). Wtedy ani Ty przypadkiem, ani
token roju nie wypchnie niczego prosto do `main`.

---

## 4. [ADMIN] Konto `roj` na VPS i limity

```bash
sudo adduser --disabled-password --gecos "" roj
sudo loginctl enable-linger roj
grep ^roj: /etc/subuid /etc/subgid                         # → wklej (muszą być 2 wpisy)
ls /etc/systemd/system/user@.service.d/delegate.conf       # istnieje od api-gate; jeśli nie: sekcja 6.3 tutorialu api-gate
sudo systemctl set-property user-$(id -u roj).slice CPUQuota=100% MemoryMax=2G MemorySwapMax=0 TasksMax=1024
systemctl show user-$(id -u roj).slice -p CPUQuotaPerSecUSec -p MemoryMax    # → wklej
ss -ltn | grep 18732 || echo "port 18732 wolny"
sudo machinectl shell roj@
```

Wszystko od sekcji 5 do 17 robisz jako `roj` (w tej powłoce). Wyjście: `exit`.

**Sprawdzenie:** `whoami` → `roj`; `podman info | grep -iE "rootless|cgroupVersion"` → `rootless: true`, `cgroupVersion: v2` → wklej.

---

## 5. [roj] Kontener: pliki, sekrety, pierwszy start

```bash
mkdir -p ~/data ~/.config/containers/systemd
curl -fsSL https://raw.githubusercontent.com/<login>/roj/main/deploy/podman/roj.container \
  -o ~/.config/containers/systemd/roj.container
sed -i 's#OWNER#<login>#' ~/.config/containers/systemd/roj.container
curl -fsSL https://raw.githubusercontent.com/<login>/roj/main/deploy/podman/roj.env.example -o ~/roj.env
chmod 600 ~/roj.env
nano ~/roj.env
```

### 5.1 Sekrety: wszystkie, jakie istnieją

| zmienna w `~/roj.env` | obowiązkowa | wartość |
|---|---|---|
| `ROJ_API_KEY` | **tak** | klucz `sk-gate-…` utworzony dla roj w panelu api-gate |
| `ROJ_TELEGRAM_TOKEN` | nie | token bota od @BotFather (sekcja 7) |
| `ROJ_GITHUB_TOKEN` | nie | token do pull requestów (sekcja 14) |

Nic więcej. **Żaden sekret nie trafia do `roj.toml`**, do repozytorium ani do czatu.
Puste linie `ROJ_TELEGRAM_TOKEN=` i `ROJ_GITHUB_TOKEN=` mogą zostać puste.

### 5.2 Pierwszy start

```bash
podman pull ghcr.io/<login>/roj:latest
systemctl --user daemon-reload
systemctl --user start roj.service
systemctl --user status roj.service --no-pager | head -8      # → wklej
podman ps                                                     # → wklej (STATUS: Up … (healthy))
ls -la ~/data                                                 # → wklej
```

Quadlet sam włącza usługę przy starcie systemu (`WantedBy=default.target`); `enable` nie jest potrzebne.

Pierwszy start tworzy w `~/data`: `roj.toml` (szablon), `roj.db`, `serve.heartbeat`,
`panel_token`, `logs/`. Pliki należą do `roj` (dzięki `UserNS=keep-id`), więc edytujesz je
zwykłym `nano`.

Skrót do CLI (dopisz do `~/.bashrc`, potem `source ~/.bashrc`):

```bash
alias roj='podman exec -it roj roj'
```

### 5.3 Sprawdzenie: czy obraz działa na tym serwerze (bez tokenów)

```bash
roj --version                 # roj 0.4.x
roj healthcheck               # heartbeat 12s old   (kod wyjścia 0)
roj selftest                  # → wklej
```

Oczekiwany wynik `roj selftest` (czasy będą inne):

```
roj selftest: 9/9 passed (offline, stand-in models, no tokens)
[PASS] setup: home, project, git repo, gateway               0.3s  ...
[PASS] mission: goal -> Architect's questions                0.0s  2 questions from the Architect
[PASS] mission: stage plan, lint, critique, owner gate       0.0s  plan ready after 2 attempt(s), waiting for the owner
[PASS] mission: team, Leader review, report                  0.0s  stage done, knowledge pack written, ...
[PASS] chat: owner -> Leader                                 0.0s  reply received, directive recorded
[PASS] selfdev: change request -> gates -> pull request      1.0s  worktree build, gates green, review approve, ...
[PASS] lab: model cards, A/B, Playbook                       0.4s  study report written, Playbook v1 active, ...
[PASS] telegram: delivery                                    0.1s  5 message(s) delivered
[PASS] ops: backup, export, event log                        0.2s  backup ..., export ... and no secrets, event chain ok
```

Selftest działa w tymczasowym katalogu w `/tmp` kontenera, na sztucznej bramce, sztucznym
GitHubie i sztucznym Telegramie. Nie dotyka `~/data`, nie wydaje tokenów, nie łączy się z siecią.
Sprawdza: Pythona, SQLite, git, pytest, prawa zapisu, cały łańcuch misji i samodoskonalenia.
Każdy `FAIL` → wklej całą linię.

---

## 6. [roj] Konfiguracja `roj.toml`: bramka, modele, ceny

### 6.1 Wzór

```bash
cp ~/data/roj.toml ~/data/roj.toml.initial
podman exec roj cat /opt/roj/examples/roj.server.toml.example > ~/data/roj.toml
nano ~/data/roj.toml
```

`[gateway] base_url = "http://127.0.0.1:18731/v1"` jest już ustawione (api-gate na tym hoście).

### 6.2 Identyfikatory modeli

```bash
systemctl --user restart roj.service
roj models --refresh            # → wklej (lista id z bramki)
```

Wpisz **dokładne** id z tej listy w sekcje `[models.<rola>]`. Role i zalecenia:

| rola | kto ją pełni | zalecenie |
|---|---|---|
| `architect` | Architekt misji: plan etapu, zespół | najsilniejszy model |
| `critic` | Przewódca misji (jeśli projekt nie ma `[leader]`), recenzent diffów | silny, **inna rodzina niż `architect` i `coder`** |
| `planner` | zapas dla Architekta, planista selfdev | silny |
| `analyst` | domyślny agent etapu | średni |
| `coder` | agent piszący kod w selfdev | najlepszy w kodzie |
| `checker` | weryfikator | inna rodzina niż `analyst` |
| `classifier` | tanie klasyfikacje | najtańszy sensowny |
| `summarizer` | skracanie długich rozmów | tani, precyzyjny |
| `researcher` | Lab: czytanie poradników dostawców | średni |

Każda rola: `model = "…"` i `fallbacks = ["…", "…"]` (najlepiej z innej rodziny).
`roj doctor` ostrzeże, jeśli recenzent jest z tej samej rodziny co autor.

### 6.3 Ceny (ważne)

Model bez ceny jest liczony po celowo drogiej cenie domyślnej 15/75 USD za 1M tokenów. Przy
limicie 5 USD dziennie na projekt jedno badanie Lab może wtedy zatrzymać się na budżecie
(`paused_budget`) po kilkuset tysiącach tokenów. Ceny z bramki:

```bash
exit                                          # z powrotem do [ADMIN]
sudo machinectl shell apigate@
podman exec apigate apigate models            # kolumna price=in/out (+markup)
exit
sudo machinectl shell roj@
```

i dla każdego używanego modelu w `roj.toml`:

```toml
[pricing.models."<id albo wzorzec*>"]
input = 3.0          # USD za 1M tokenów wejścia
cached_input = 0.3   # jeśli dostawca daje rabat za cache; inaczej usuń linię
output = 15.0
```

### 6.4 Samodoskonalenie

```toml
[selfdev]
clone_url = "https://github.com/<login>/roj.git"
github_repo = "<login>/roj"
```

### 6.5 Zastosuj i sprawdź

```bash
systemctl --user restart roj.service
roj doctor --live              # → wklej
```

`--live` wysyła po 2 bardzo małe zapytania do każdego przypisanego modelu (sprawdza cache
dostawcy), `getMe` Telegrama i pobiera jeden publiczny kanał RSS.

Oczekiwane: **zero `FAIL`**. Linie `WARN`, które na tym etapie są w porządku:
`Telegram not configured` (jeśli pomijasz sekcję 7), `no [storage] backup_dir` (sekcja 10),
`pull requests …: token … not set` (sekcja 14), `FRED_API_KEY not set` (dotyczy tylko
przykładowego przeglądu rynków). Każde `no price for …` popraw w 6.3. W liniach `live`
interesuje mnie: czas odpowiedzi, `usage reported` i `prompt cache yes/not reported`.

---

## 7. [roj] Telegram (opcjonalnie)

1. @BotFather → `/newbot` → token. Wpisz go do `~/roj.env` jako `ROJ_TELEGRAM_TOKEN=…`.
2. Napisz do swojego bota dowolną wiadomość.
3. W przeglądarce: `https://api.telegram.org/bot<TOKEN>/getUpdates` → skopiuj liczbę
   `"chat":{"id": …}`. (Działa, bo roj nie czyta Telegrama, dopóki nie ma `telegram_chat_id`.)
4. W `~/data/roj.toml`: `[notify] telegram_chat_id = "<ta liczba>"`.

```bash
systemctl --user restart roj.service
roj telegram test
```

**Sprawdzenie:** wiadomość od bota przyszła. Bot przyjmuje kliknięcia **tylko** z tego czatu.

---

## 8. [roj] Automatyczna aktualizacja co 2 minuty

```bash
mkdir -p ~/.config/systemd/user/podman-auto-update.timer.d
cat > ~/.config/systemd/user/podman-auto-update.timer.d/override.conf <<'EOT'
[Timer]
OnCalendar=
OnCalendar=*:0/2
RandomizedDelaySec=30
Persistent=true
EOT
systemctl --user daemon-reload
systemctl --user enable --now podman-auto-update.timer
systemctl --user list-timers | grep auto-update        # → wklej
podman auto-update --dry-run                           # → wklej
```

**Sprawdzenie:** w `--dry-run` widać `roj.service` z obrazem `ghcr.io/<login>/roj:latest`
i `UPDATED: false`. Pełny test aktualizacji jest w sekcji 14.4.

Przypięcie wersji / ręczny powrót: w `roj.container` zamień `:latest` na `:<sha commita>`,
usuń linię `AutoUpdate=registry`, potem `systemctl --user daemon-reload && systemctl --user restart roj.service`.

---

## 9. [WINDOWS] Panel przez tunel SSH

Panel słucha tylko na `127.0.0.1:18732` serwera i celowo **nie** jest wystawiony przez nginx.

```powershell
ssh -L 18732:127.0.0.1:18732 <admin>@<adres VPS>
```

Na serwerze (w tej sesji): `sudo cat /home/roj/data/panel_token`. W przeglądarce na Windows:
`http://127.0.0.1:18732/?token=<token>`.

**Sprawdzenie:** strona się otwiera; zakładki zatwierdzeń, zadań, logów, raportów, budżetów.
Misji w panelu jeszcze nie ma: misje obsługujesz z CLI, zatwierdzenia także z panelu i Telegramu.

---

## 10. [roj] Kopie zapasowe

Bazy (SQLite) zostają na lokalnym dysku w `~/data`. **Nie umieszczaj `~/data` na zamontowanym
buckecie**: sieciowe systemy plików psują blokady SQLite.

Na bucket idą kopie:

1. [ADMIN] Zamontuj bucket (SFTPGo) w `/mnt/roj-backup` tak, jak robisz to dla innych
   projektów, z prawem zapisu dla użytkownika `roj` → wklej `ls -ld /mnt/roj-backup`.
2. [roj] W `roj.container` odkomentuj `Volume=/mnt/roj-backup:/backup`; w `roj.toml`:
   `[storage] backup_dir = "/backup"`.

```bash
systemctl --user daemon-reload && systemctl --user restart roj.service
roj backup                       # backup: /backup/roj-backup-YYYYMMDD-HHMMSS.tar.gz
ls -la /mnt/roj-backup           # plik jest w buckecie
```

`roj serve` robi jedną kopię dziennie i trzyma 14 ostatnich. Bez bucketu kopie lądują w
`~/data/backups` (ten sam dysk: lepsze to niż nic).

**Test odtworzenia (raz, zalecany):**
```bash
mkdir -p /tmp/restore && tar xzf /mnt/roj-backup/roj-backup-*.tar.gz -C /tmp/restore --strip-components=1
ls /tmp/restore                  # roj.db, roj.toml, projects/...
rm -rf /tmp/restore
```
Prawdziwe odtworzenie: zatrzymaj usługę, rozpakuj kopię do pustego `~/data`, uruchom.

---

## 11. Pierwsze prawdziwe wywołanie modelu

```bash
roj project create lab
roj ask -p lab -m <najtańszy id> "Reply with the single word OK."
roj budget show -p lab
roj stats -p lab
```

**Sprawdzenie:**
- `project create` wypisuje `tokens_per_day: 1000000`, `usd_per_day: 5.0`;
- `ask` wypisuje odpowiedź i linię `-- <model> | in N (cached 0) out M | $0.0000…`;
  jeśli zamiast liczb tokenów widać szacunek, bramka nie zwraca `usage` → wklej;
- `budget show` pokazuje zużyte tokeny i USD projektu `lab`.

---

## 12. Lab: karty modeli i Playbook

Lab mierzy, co umie każdy model i jak każda rodzina woli dostawać prompty (6 pokręteł:
język pl/en, struktura XML/markdown, dokumenty przed/po zadaniu, przypomnienie na końcu,
krótkie rozumowanie, przykłady). Wynik liczy kod, nie model.

### 12.1 Najpierw małe badanie (kontrola kosztu)

```bash
roj lab study -p lab --models <id1>,<id2> --per-kind 1 --knobs reasoning,language
```

Przed startem Lab liczy szacunek tokenów; jeśli przekracza `--budget-tokens` (domyślnie 400 000),
sam się zmniejsza albo zatrzymuje z komunikatem. Wynik kończy się `"status": "waiting_approval"`.

**Sprawdzenie:**
```bash
ls ~/data/projects/lab/reports/              # study-YYYY-MM-DD-xxxxxxxx.md
less ~/data/projects/lab/reports/study-*.md
roj stats -p lab                             # ile naprawdę kosztowało
```
W raporcie: karty modeli (wynik, tokeny na przypadek, opóźnienie, cache, wywoływanie narzędzi,
najsłabsze rodzaje zadań), tabela eksperymentów z przedziałami, potwierdzenie na przypadkach
odłożonych, proponowane profile. → wklej mi raport (to pierwsze prawdziwe dane).

### 12.2 Zatwierdzenie

```bash
roj approvals list -p lab
roj approvals approve -p lab <ID>            # albo przycisk w Telegramie / panel
```
`roj serve` wznawia zadanie w ciągu ~30 s (albo od razu: `roj task resume -p lab`).

```bash
roj lab playbook                             # version 1, profile per rodzina
roj lab cards
```

### 12.3 Pełne badanie (później, gdy znasz koszt małego)

```bash
roj lab study -p lab                         # wszystkie przypisane modele, wszystkie pokrętła
```

Powtórzone badanie kosztuje prawie zero za niezmienione prompty (lokalny cache).

---

## 13. Pierwsza misja: „roj ulepsza roj”

Agenci znają cel projektu z `docs/CONCEPT.md` (streszczenie dostaje każdy plan). Przeczytaj
sekcję 22.5 tego pliku: tam są progi akceptacji, które zaproponowałem. Jeśli chcesz inne, zmień
je w repozytorium **przed** pierwszą misją (plik jest chroniony przed agentami, nie przed Tobą).

### 13.1 Start

```bash
roj mission start -p lab --control full "Improve the roj project using roj itself, as described in docs/CONCEPT.md. First stage: understand the code and measure baselines; no code changes in the first stage."
```

Jedno wywołanie Architekta. Wynik (JSON): `"status": "needs_answers"`, `understanding`
(co zrozumiał), `open_questions` (do 6 pytań: pytanie, dlaczego ważne, opcje, wartość domyślna).

### 13.2 Odpowiedzi

```bash
roj mission answer -p lab        # pyta po kolei; Enter = wartość domyślna
roj mission show -p lab          # "status": "ready"
```
Bez odpowiedzi planowanie nie startuje (`roj mission next` kończy się kodem 2).

### 13.3 Etap

```bash
roj mission next -p lab
```
1. Architekt planuje etap; kod sprawdza plan (istniejące narzędzia i role, mierzalne kryteria,
   brak narzędzi zmieniających kod); Przewódca krytykuje; do 3 rund.
2. Zadanie czeka: `t2.mission_stage`. W `roj approvals list -p lab` widzisz cel etapu, kryteria,
   zespół (agent, model, zadanie) i potrzeby.
3. Zatwierdzasz → w trybie `full` każdy agent czeka jeszcze na `t2.mission_agent_run`
   (zatwierdzasz każdego osobno).
4. Agenci pracują (czytają repozytorium, zapisują wiedzę do pakietów).
5. Przewódca ocenia; powstaje raport etapu; wnioski o zmianę kodu stają się zadaniami selfdev.

**Sprawdzenie:**
```bash
roj mission show -p lab                               # "status": "stage_done", stages[0].verdict
ls ~/data/projects/lab/reports/mission-*-stage-1-*.md
roj kb list -p lab                                    # pakiety wiedzy zbudowane przez agentów
roj logs show -p lab                                  # kroki, czasy, koszt per rola
roj budget show -p lab
```

### 13.4 Rozmowa

```bash
roj chat -p lab --to architect "Dlaczego taki zespół?"
roj chat -p lab --to leader "Zawsze wybieraj najtańszy model, który spełnia kryteria."
roj chat -p lab --to <nazwa agenta z etapu> "Co było najtrudniejsze?"
roj mission control -p lab partial                    # albo leads / full, w dowolnej chwili
```
Polecenie ze słowem „zawsze/nigdy” zapisuje się jako dyrektywa (`roj mission show` → `directives`)
i trafia do każdego następnego planu i oceny.

### 13.5 Następne etapy

Przeczytaj raport, odpowiedz na nowe pytania (`roj mission answer`), `roj mission next`.
Szczegółowo planowany jest zawsze tylko najbliższy etap.

---

## 14. Samodoskonalenie i pełna pętla: PR → scalenie → CI → serwer

### 14.1 Token GitHub (raz)

GitHub → *Settings → Developer settings → Fine-grained tokens → Generate new token*:
*Repository access*: **Only select repositories** → `roj`; *Permissions*: **Contents: Read and
write**, **Pull requests: Read and write**; termin ważności np. 90 dni.
Wpisz do `~/roj.env` jako `ROJ_GITHUB_TOKEN=…`, potem `systemctl --user restart roj.service`.
Przy ochronie gałęzi `main` (sekcja 3) token nie może scalić ani wypchnąć do `main`.

### 14.2 Jedno ulepszenie

Z wniosku etapu (zadanie selfdev już istnieje: `roj task list -p lab`) albo ręcznie:

```bash
roj selfdev run -p lab --goal "Repair malformed JSON from models in code before paying for a retry; measure how often it happens."
```

1. Propozycje → czeka `t2.selfdev_start` (tytuł, problem, zmiana, pliki, ryzyko) → zatwierdź.
2. Agent pracuje w osobnej kopii (`~/data/selfdev/<id>`), bramki w kodzie: niepusty diff,
   pliki chronione nietknięte, istniejące testy niezmienione, ≤ 400 linii, nowy test, cały
   zestaw testów zielony, liczba testów nie spada. Recenzja modelem innej rodziny.
3. Czeka `t3.open_pull_request` (diff jest w `~/data/projects/lab/reports/selfdev-<id>.patch`)
   → zatwierdź → pull request na GitHubie.

**Sprawdzenie:**
- `~/data/projects/lab/reports/selfdev-*.md`: lista bramek `[x]`, werdykt recenzji, link do PR;
- linia „tests ran without network: yes/no” → wklej (zależy od jądra VPS);
- na GitHubie PR z gałęzi `roj/selfdev-…`, CI `ci` uruchomione na PR.

### 14.3 Ty decydujesz

Przejrzyj PR na GitHubie. Scal albo zamknij. Nic nie trafia na serwer bez scalenia.

### 14.4 Sprawdzenie pełnej pętli wdrożenia

Po scaleniu: Actions buduje obraz (kilka minut), serwer pobiera go w ciągu ~2 minut.

```bash
journalctl --user -u podman-auto-update.service -n 20 --no-pager      # → wklej
podman image inspect "$(podman inspect roj --format '{{.Image}}')" \
  --format '{{index .Labels "org.opencontainers.image.revision"}}'
```
Druga komenda wypisuje sha commita, z którego zbudowany jest **działający** kontener: musi być
równy sha commita scalenia z GitHuba. Potem `roj selftest` (9/9) i `roj healthcheck`.

Jeśli nowy obraz nie wstanie (health-check), `podman auto-update` sam wraca do poprzedniego.

---

## 15. Odporność: restart, budżet, awaria modelu

Opcjonalne, ale warto raz zobaczyć:

| Test | Jak | Oczekiwane |
|---|---|---|
| Restart w trakcie zadania | podczas `roj lab study` (z drugiej sesji): `systemctl --user restart roj.service` | polecenie w pierwszej sesji zostaje przerwane; w ciągu ~30 s po starcie `roj serve` sam wznawia zadanie od ostatniego ukończonego kroku (`roj task list -p lab`, `roj logs show -p lab`: `task.resumed`); opłacone wyniki nie są płacone drugi raz |
| Budżet | `roj project create budgettest --usd 0.01`, potem małe `roj lab study -p budgettest --models <id> --per-kind 1 --knobs reasoning` | zadanie `paused_budget`, nie `failed`; `roj budget show -p budgettest` |
| Awaria modelu | `roj ask -p lab -m nie-ma-takiego-modelu -f <dobre id> "Reply with OK"` | odpowiedź przychodzi z zamiennika; ostatnia linia kończy się `fallback #1` |
| Integralność logu | `roj doctor` | `event log N events, chain ok` dla każdego projektu |

---

## 16. Praca codzienna: harmonogramy i flagi

Dopiero gdy pierwsze ręczne przebiegi wyglądają dobrze:

```bash
podman exec roj cat /opt/roj/examples/project.lab.toml.example     # wzór
nano ~/data/projects/lab/project.toml                              # dopisz [[schedules]]
systemctl --user restart roj.service
roj schedule list -p lab
```
Wzór: jedno ulepszenie co noc (02:30), badanie Lab w niedzielę (04:00). Każdy krok z efektami
nadal czeka na Ciebie. Uwaga: niezatwierdzone propozycje z kolejnych nocy się kumulują.

Flagi w `roj.toml` `[permissions]` przełączaj **pojedynczo**, gdy historia zatwierdzeń to
uzasadnia, np. `"t2.mission_stage" = "auto"`. Scalanie PR zostaje Twoje (ochrona gałęzi).

---

## 17. Logi dla wspólnego przeglądu

```bash
roj logs export --days 2          # wypisuje /data/exports/roj-diagnostics-….zip
ls ~/data/exports/                # ten sam plik na hoście
```
[WINDOWS]:
```powershell
scp <admin>@<adres VPS>:/home/roj/data/exports/roj-diagnostics-*.zip D:\la2-idea\agent-system\roj-logs\
```
(Jeśli `<admin>` nie ma prawa odczytu `/home/roj`, skopiuj plik najpierw: `sudo cp … /tmp/ && sudo chown <admin> /tmp/roj-diagnostics-*.zip`.)

W zipie: logi zadań (`.log` czytelne, `.jsonl` surowe), ustawienia, stan projektów, raporty,
Playbook, wszystkie prompty i odpowiedzi z okresu. Bez kluczy i tokenów (sprawdza to też
`roj selftest`). `--no-prompts` pomija prompty i odpowiedzi.

---

## 18. Rozwiązywanie problemów

| Objaw | Co sprawdzić |
|---|---|
| `image` w Actions czerwone na *Smoke test* | log kroku → wklej; najczęściej brakujący pakiet w obrazie albo błąd selftestu |
| `podman pull` 401/denied | pakiet `roj` nie jest publiczny (sekcja 3) albo `<login>` nie jest małymi literami |
| usługa nie wstaje, `start operation timed out` | `podman logs roj`, `journalctl --user -u roj -n 50 --no-pager` → wklej; health-check czeka na `serve.heartbeat` w `~/data` |
| `Permission denied` na `/data` | `ls -la ~/data` → właściciel musi być `roj`; katalog musi istnieć przed startem |
| `doctor`: gateway FAIL | `curl -s http://127.0.0.1:18731/v1/models -H "Authorization: Bearer $(grep ROJ_API_KEY ~/roj.env | cut -d= -f2)" | head -c 300` → wklej (bez klucza!) |
| `doctor`: `none of [...] on the gateway` | id w `[models.*]` różni się od `roj models --refresh` |
| zadanie stoi w `paused_budget` | `roj budget show -p lab`; brak cen (6.3) albo limit dnia; `roj task resume -p lab --include-paused` po zmianie |
| zadanie stoi w `paused_api` | wszystkie modele roli zawiodły; `roj models`, `roj logs show -p lab`; po naprawie `roj task resume` |
| zatwierdziłem, nic się nie dzieje | `roj serve` wznawia co ~30 s; jeśli wykonuje długie zadanie, czeka (znane ograniczenie); `roj task resume -p lab` z CLI |
| Telegram milczy | `roj telegram test`; `telegram_chat_id` to liczba z `getUpdates`; token w `~/roj.env`; restart po zmianie |
| selfdev: `git clone` nie działa | `clone_url` w `[selfdev]`; repozytorium publiczne albo `ROJ_GITHUB_TOKEN` ustawiony |
| selfdev: brak PR, jest tylko patch | brak `github_repo` albo `ROJ_GITHUB_TOKEN`; patch jest w `reports/` |
| po scaleniu serwer ma stary kod | Actions zielone? `podman auto-update --dry-run`; `AutoUpdate=registry` w `roj.container` |
| plan etapu odrzucony 3 razy | raport błędu w `roj logs show -p lab` (zdarzenia `mission.plan_revision`) → wklej |

---

## 19. Lista kontrolna

| # | Sprawdzenie | Gdzie | Wynik |
|---|---|---|---|
| 1 | CI na Linuksie zielone, `roj selftest: 9/9` w zadaniu `image` | GitHub Actions | ☐ |
| 2 | Pakiet `ghcr.io/<login>/roj` publiczny | GitHub Packages | ☐ |
| 3 | `podman ps`: `roj` *healthy* | VPS | ☐ |
| 4 | `roj selftest` 9/9 na serwerze | VPS | ☐ |
| 5 | `roj doctor --live`: 0 FAIL, wszystkie modele odpowiadają | VPS | ☐ |
| 6 | Ceny wszystkich używanych modeli wpisane (brak `no price for`) | `roj doctor` | ☐ |
| 7 | `roj telegram test` dochodzi (jeśli używasz) | telefon | ☐ |
| 8 | Timer auto-update aktywny, `--dry-run` widzi `roj` | VPS | ☐ |
| 9 | Panel otwiera się przez tunel | Windows | ☐ |
| 10 | `roj backup` tworzy plik w buckecie; test odtworzenia | VPS | ☐ |
| 11 | `roj ask` zwraca odpowiedź z prawdziwymi licznikami tokenów | VPS | ☐ |
| 12 | Małe badanie Lab: raport, zatwierdzenie, `roj lab playbook` v1 | VPS | ☐ |
| 13 | Misja: pytania → odpowiedzi → etap 1 zatwierdzony → raport etapu | VPS | ☐ |
| 14 | `roj chat` z Architektem, Przewódcą i agentem (tryb `full`) | VPS | ☐ |
| 15 | Selfdev: PR na GitHubie z zielonym CI | GitHub | ☐ |
| 16 | Po scaleniu: rewizja działającego kontenera = sha scalenia | VPS | ☐ |
| 17 | `roj logs export` → zip przesłany do wspólnego przeglądu | Windows | ☐ |

Po punktach 1–11 system **działa**. Po 12–16 działa **pętla ulepszania**. Punkt 17 to
materiał, na którym ustalimy pierwsze prawdziwe wartości metryk (CONCEPT.md, sekcja 22.3).
