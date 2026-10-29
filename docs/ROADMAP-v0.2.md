# Roadmap v0.2 — laboratório autônomo

Documento de trabalho para o agente de código aberto na raiz do `hyperv-console-mcp`, num host Windows com Hyper-V.

## 0. Contexto e objetivo

O repositório `hyperv-console-mcp` é um servidor MCP local (stdio, Python 3.11+, MCP SDK 2.x `MCPServer`) que controla VMs Hyper-V por um worker PowerShell persistente (`src/hyperv_console_mcp/bridge/HyperVBridge.ps1`), com dashboard tkinter, broker de aprovação por named pipe (`ipc.py`), eventos (`events.py`), console serial (`serial.py`) e um "driver lab" (`lab/`).

O objetivo desta rodada é transformar o projeto num laboratório de desenvolvimento de drivers totalmente operável por IA, numa VM Windows descartável e allowlistada:

1. Corrigir todos os bugs conhecidos (Fase 1).
2. Ter dois transportes de guest: PowerShell Direct com sessão persistente (padrão) e SSH (opcional), escolhíveis por configuração (Fase 2).
3. Dashboard e ferramentas MCP para preparar o guest para automação: sem tela de login (autologon), sem bloqueio de tela, sem prompts de UAC, senha gerenciada pelo host (Fase 3).
4. Um perfil de laboratório autônomo em que a IA executa o ciclo completo (checkpoint → stage → run → debug → coleta → revert) sem cliques humanos (Fase 4).
5. Um MCP de WinDbg/KD bem feito, integrado ao ciclo de vida da VM (Fase 5).
6. Testes, CI e documentação à altura (Fase 6).

### Limites que continuam valendo mesmo com "controle total"

"Controle total" significa controle total sobre as VMs da allowlist. Continua obrigatório:

- `HYPERV_MCP_ALLOWED_VMS` não vazio para qualquer mutação; nada fora da allowlist é tocado.
- No host, só são permitidas mudanças em: objetos Hyper-V das VMs allowlistadas, um switch interno dedicado (nunca externo), regras de firewall restritas a esse switch/porta, e arquivos sob as raízes configuradas (`%LOCALAPPDATA%\HyperVConsoleMCP\...`, `HYPERV_MCP_LAB_ROOT` etc.).
- Nunca carregar driver, rodar payload ou mudar Secure Boot/HVCI/test-signing **no host**. Tudo isso só no guest.
- Segredos (senha do guest, chave SSH, chave KDNET) nunca aparecem em argumentos de ferramenta, resultados, eventos ou auditoria.

## 1. Regras de trabalho

- Crie o branch `v0.2-autonomous-lab`. Commits pequenos e temáticos (uma fase ou subfase por commit), mensagens em inglês.
- Antes de cada commit: `python -m pytest`, `python -m ruff check src tests scripts`, e checagem de sintaxe de todos os `.ps1`/`.psm1` (o mesmo script do CI). Nada vermelho.
- Todo bug corrigido ganha um teste de regressão que falharia antes da correção. Para o bridge, use o harness de `tests/test_bridge_regressions.py` (wrapper com mocks + `PowerShellBackend`).
- Testes Windows-only devem ter `skipif` correto para rodar limpo em Linux também.
- Validação live: só com `HYPERV_MCP_LIVE_TEST_VM` definido apontando para uma VM descartável; scripts em `tests/live_*.py`, nunca no pytest padrão.
- Não invente APIs. Quando depender de comportamento do Windows/Hyper-V/WinDbg que não dá para testar com mock, escreva um teste live opcional e registre a dúvida em `docs/PROJECT.md`.
- Ao final de cada fase, atualize `docs/PROJECT.md` (changelog honesto: o que foi testado de verdade e o que não foi) e a contagem de ferramentas no README.

## 2. Fase 1 — Correções (fazer primeiro, nesta ordem)

### 2.1 Bridge PowerShell

- `Invoke-GuestQuery` recebe `-ArgumentList` mas declara `-Arguments`. `guest_read_file`, `guest_write_file`, `guest_read_chunk` e `guest_assemble_file` (`HyperVBridge.ps1` ~382/406/443/476) falham com "A parameter cannot be found that matches parameter name 'ArgumentList'". Padronize para `-Arguments` (e/ou `[Alias('ArgumentList')]`). Teste de regressão chamando cada uma dessas operações pelo bridge real com `Invoke-Command` mockado.
- `guest_exec` reporta `success=true` com erro de cmdlet. Com `ErrorActionPreference='Continue'`, `Get-Item C:\nao-existe` passa como sucesso. Registre `$Error.Count` antes/depois (ou `$?` por pipeline), retorne `had_errors` e `error_records` (mensagens truncadas), e `success = exit_code == 0 -and -not had_errors`. Preserve a correção de stderr nativo existente.
- `vm_power` shutdown/save bloqueante. `Stop-VM` gracioso pode passar do timeout de 90 s e matar o worker. Faça as ações de energia retornarem imediatamente (`-AsJob` ou equivalente) com `{"accepted": true, "operation_id": ...}` e deixe `vm_wait` / uma nova ferramenta `operation_status` acompanharem. `turn_off`/`reset` podem continuar síncronos.
- Valide o bridge com PSScriptAnalyzer no CI (regras padrão, sem suprimir em massa).

### 2.2 Driver lab

- `Initialize-DriverLab.ps1`: `param([Parameter(Mandatory)][string]$Root = 'C:\DriverLab')` — `Mandatory` ignora o default e falha em sessão não interativa. Remova `Mandatory`.
- Hash canônico Python × PowerShell 5.1. `ConvertTo-Json` do 5.1 provavelmente escapa `& ' < >` (`\u0026` etc.) e `Sort-Object` usa ordenação cultural. Restrinja caminhos de pacote a `[A-Za-z0-9._-]` por segmento (separador `/`) nos dois lados, implemente o escape JSON de strings manualmente no `.psm1` e ordene chaves com `[StringComparer]::Ordinal`. Teste: mesmo manifesto → mesmo hash em Python e em `powershell.exe` 5.1 (teste Windows-only).
- Verificação incompleta. `Test-DriverLabPackage.ps1` deve falhar se houver arquivos no diretório do pacote que não estão no manifesto; `Invoke-DriverLabRun.ps1` deve exigir que `Run-Lab.ps1` esteja listado no manifesto.
- Staging não reexecutável. Faça o staging inteiro em `C:\DriverLab\Transfers\<transfer-id>\<package-id>` e mova o diretório para `Packages\` só no fim, com tudo verificado. Falha no meio = nada em `Packages\`. Limpe transfers órfãos com mais de 24 h no `lab_init`.
- `initialize()` (`lab/cli.py`) trata qualquer `bridge_error` na leitura como "arquivo ausente". Distinga `not_found` de outros erros (o bridge já classifica `ObjectNotFound`; faça `FileNotFoundException` no guest virar `not_found`).
- `lab_stage`/`lab_collect` leem `os.environ` diretamente. Mova `HYPERV_MCP_LAB_ROOT`, `HYPERV_MCP_EVIDENCE_ROOT` e `HYPERV_MCP_EVENT_DIRECTORY` para `Settings` e valide na inicialização.

### 2.3 Aprovação e IPC

- Broker morre após 4 aprovações. Em `ipc.py:_serve`, depois de responder: `FlushFileBuffers` + `DisconnectNamedPipe`, e só então voltar ao `ConnectNamedPipe`. Erro numa requisição → desconecta e continue, nunca `break`. Teste Windows-only com 20 aprovações sequenciais e 4 concorrentes.
- Pipe squatting. Crie a primeira instância com `FILE_FLAG_FIRST_PIPE_INSTANCE` (falhar se o nome já existir). No cliente, use `GetNamedPipeServerProcessId`, abra o processo e confirme que o executável é o Python/dashboard esperado e que o dono do token é o usuário atual; senão, negue.
- Aprovação por ferramenta, não por operação do bridge. Hoje `lab_stage` pede uma aprovação por arquivo/chunk. Mova o portão para o nível da ferramenta MCP (decorator no `server.py` ou no service), com um contextvar `approved_scope`: chamadas internas herdam a aprovação. `lab_verify`/`lab_diagnostics` (READ_ONLY) não pedem aprovação.
- Aprovação informada. O pedido leva um summary sem segredos gerado por cada ferramenta: ação de energia, ID/nome do checkpoint, caminho, primeiras ~20 linhas do comando do `guest_exec` (+ SHA-256 do comando inteiro), tamanho/hashes do pacote. O diálogo do dashboard mostra isso (com rolagem) em vez de "veja a conversa".

### 2.4 Eventos, testes, CI

- Eventos: troque "um arquivo JSON por evento" por JSONL diário (`events-YYYYMMDD.jsonl`, append com lock), retenção configurável (padrão 14 dias), e o dashboard lê incrementalmente por offset em vez de glob+sort a cada 1,5 s.
- ruff: fixe versão exata em dev (ex.: `ruff==0.17.3`), configure `[tool.ruff] src = ["src"]` e rode `ruff check --fix`. Hoje o CI quebraria com I001 em 11 testes.
- `skipif` em `tests/test_events.py` (aprovação real), no teste de mídia com caminho `C:/...` e `importorskip("tkinter")` no `test_dashboard.py`.

## 3. Fase 2 — Transporte de guest: PowerShell Direct persistente + SSH

### 3.1 Abstração

Crie `src/hyperv_console_mcp/transport/` com uma interface `GuestTransport`:

```python
class GuestTransport(Protocol):
    name: str  # "psdirect" | "ssh"
    def exec(self, vm: str, command: str, *, timeout: float, max_output: int) -> ExecResult: ...
    def put_file(self, vm: str, local: Path, remote: str, *, overwrite: bool) -> FileResult: ...
    def get_file(self, vm: str, remote: str, local: Path) -> FileResult: ...
    def health(self, vm: str) -> dict: ...
    def close(self, vm: str | None = None) -> None: ...
```

Todas as ferramentas `guest_*` e o lab passam a usar a interface. Todo resultado inclui `"transport": "<nome>"`.

Configuração:

- `HYPERV_MCP_GUEST_TRANSPORT=auto|psdirect|ssh` (padrão `auto`), sobrescrevível por VM com `HYPERV_MCP_GUEST_TRANSPORT_<VMNAME>` ou num arquivo `vms.toml` em `%LOCALAPPDATA%\HyperVConsoleMCP\` (escolha um e documente).
- `auto`: usa SSH se a VM tiver SSH provisionado por esta ferramenta e o health responder em < 2 s; senão PowerShell Direct. Nunca cai silenciosamente de `ssh` explícito para outro transporte.

### 3.2 PowerShell Direct persistente (padrão)

- O worker mantém um `New-PSSession -VMName` por VM (dicionário no `.ps1`), reutilizado entre requisições. Reconecta quando a sessão estiver `Broken`/`Closed` ou a VM tiver reiniciado (compare Uptime/boot ID). Fecha tudo no shutdown do worker.
- Arquivos via `Copy-Item -ToSession $s` / `-FromSession $s` em vez de base64 dentro de JSON. Verificação por SHA-256 dos dois lados. Isso substitui `guest_write_file` em chunks e `guest_assemble_file` no lab — mantenha as ferramentas MCP `guest_read_file`/`guest_write_file` para conteúdos pequenos (≤ 64 KiB inline), e adicione `guest_put_file`/`guest_get_file` que trabalham com caminhos sob raízes do host.
- Considere um segundo worker só para operações de guest, para um `guest_exec` longo não bloquear `vm_get`/console.
- Benchmark reproduzível (`scripts/bench_transport.py`): 10 comandos pequenos, 1 MiB e 100 MiB de ida e volta, por transporte. Resultado em `docs/PERFORMANCE.md` com hardware e data. Sem números inventados.

### 3.3 SSH (opcional)

- Cliente: biblioteca Python (`paramiko` ou `asyncssh`, escolha e justifique), conexão persistente por VM com keepalive. Não use `ssh.exe` do Windows para sessão persistente: o Win32-OpenSSH não suporta `ControlMaster`. Arquivos por SFTP.
- Provisionamento automático — ferramenta `guest_ssh_provision(vm_name, confirm_vm)`, executada via PowerShell Direct:
  - Rede: cria (se não existir) um switch interno `HyperV-MCP-Lab` e, se a VM não tiver, adiciona um adaptador nele. Configura IP estático no vNIC do host e no guest numa sub-rede privada configurável (padrão `10.77.0.0/24`, host `.1`). Nunca usa switch externo. Se a VM já tiver rede, pergunta via parâmetro (`use_existing_adapter`).
  - sshd: verifica se `sshd` já existe (Windows Server 2025 traz instalado e desativado). Se não, tenta `Add-WindowsCapability -Online -Name OpenSSH.Server~~~~0.0.1.0`; como a VM pode estar sem internet, implemente o fallback: o host baixa uma versão fixada do release oficial `PowerShell/Win32-OpenSSH` (versão e SHA-256 fixados no código), copia via `Copy-Item -ToSession` e roda `install-sshd.ps1`.
  - Chaves: gera um par ed25519 por VM no host (`%LOCALAPPDATA%\HyperVConsoleMCP\ssh\<vm>\`, ACL só do usuário atual). Instala a pública em `C:\ProgramData\ssh\administrators_authorized_keys` (para conta administradora) com ACL só Administrators e SYSTEM. Desativa `PasswordAuthentication` no `sshd_config`.
  - Host key pinning: lê `ssh_host_ed25519_key.pub` do guest pelo PowerShell Direct e grava num `known_hosts` próprio. Conexão SSH com verificação estrita; nada de TOFU.
  - Firewall do guest: regra de entrada TCP 22 só a partir do IP do host no switch interno.
  - Shell padrão do sshd = `powershell.exe` (registro `HKLM:\SOFTWARE\OpenSSH\DefaultShell`), para os comandos serem os mesmos dos dois transportes.
- Também: `guest_ssh_status` (READ_ONLY) e `guest_ssh_deprovision` (remove regra, chave, adaptador se foi criado pela ferramenta).

## 4. Fase 3 — Preparar o guest para automação (dashboard + MCP)

Crie uma página "Guest access" no dashboard e as ferramentas MCP equivalentes. Toda mudança é registrada num arquivo de estado no guest (`C:\DriverLab\State\guest-config.json`) com o valor anterior, e existe `guest_config_revert` que restaura tudo.

### 4.1 Senha gerenciada pelo host

- Remova a dependência de `HYPERV_MCP_GUEST_PASSWORD` em variável de ambiente como caminho principal. Guarde a credencial do guest no Windows Credential Manager do usuário atual (DPAPI), alvo `HyperVConsoleMCP/<vm>`. Env var continua aceita como fallback, com aviso.
- `guest_password_rotate(vm_name, confirm_vm)`: gera senha aleatória forte, troca no guest via PowerShell Direct, valida uma nova sessão com a senha nova antes de gravar no Credential Manager, e só então descarta a antiga. Se a validação falhar, reverte.
- Dashboard: botões "Definir credencial", "Rotacionar senha", "Testar acesso (PS Direct / SSH)".

### 4.2 "Tirar a senha de login" — sem prompt de login no console

O objetivo é a VM ligar direto na área de trabalho e nunca travar, para o console sintético e as capturas de tela funcionarem sem interação. Implemente `guest_autologon_configure(vm_name, enabled, confirm_vm)`:

- Conta local (não Microsoft account). `HKLM\SOFTWARE\Microsoft\Windows NT\CurrentVersion\Winlogon`: `AutoAdminLogon=1`, `DefaultUserName`, `DefaultDomainName=..`
- Senha do autologon como segredo LSA `DefaultPassword` via `LsaStorePrivateData` (o mesmo método do Sysinternals Autologon), não em texto puro no registro. Ao desativar, apague o segredo LSA e qualquer `DefaultPassword` residual no registro.
- Windows 10/11: `HKLM\SOFTWARE\Microsoft\Windows NT\CurrentVersion\PasswordLess\Device\DevicePasswordLessBuildVersion=0`, senão o autologon pode ser ignorado.
- Não defina senha em branco como solução padrão: por padrão o Windows restringe contas sem senha a logon de console (`LimitBlankPasswordUse`), o que tende a quebrar PowerShell Direct e SSH. Se quiser oferecer "senha em branco" como opção avançada, ela deve testar PS Direct e SSH logo após aplicar e reverter automaticamente se algum deixar de funcionar.

Opções adicionais, cada uma um toggle independente com revert:

- Sem tela de bloqueio: `HKLM\SOFTWARE\Policies\Microsoft\Windows\Personalization\NoLockScreen=1`.
- Sem Ctrl+Alt+Del no logon: `DisableCAD=1` em `...\Policies\System`.
- Sem suspensão/desligar tela/descanso de tela e sem senha ao acordar: `powercfg /change standby-timeout-ac 0`, `monitor-timeout-ac 0`, `powercfg /SETACVALUEINDEX SCHEME_CURRENT SUB_NONE CONSOLELOCK 0`, desativar screensaver.
- UAC sem prompt para administradores: `ConsentPromptBehaviorAdmin=0`. Mantenha `EnableLUA=1` (desligar LUA quebra apps e muda o modelo de tokens).
- Opcional: desativar Windows Update automático e reinicializações automáticas (para a VM não reiniciar no meio de um teste).

Validação: depois de aplicar, a ferramenta reinicia a VM só se `restart=true` for passado, espera o guest e confirma via `console_screenshot` (Fase 4.4) e `query user` que há sessão interativa ativa.

## 5. Fase 4 — Perfil de laboratório autônomo

### 5.1 Perfil

`HYPERV_MCP_PROFILE=lab` liga, somente para as VMs da allowlist: todas as capacidades (ALLOW_POWER/CHECKPOINTS/GUEST/CONSOLE/SCREENSHOTS/DEBUGGER/MEDIA), aprovação automática (dashboard opcional, só observa), e as ferramentas `lab_run` e `lab_cycle`. Os campos `confirm_*` continuam existindo (são baratos e evitam a VM errada). `HYPERV_MCP_PROFILE=safe` mantém o comportamento atual. Mostre o perfil ativo no `hyperv_host_status` e no dashboard.

### 5.2 Configuração do guest para drivers

`lab_guest_configure(vm_name, confirm_vm, testsigning, kernel_debug, crash_dump, disable_hvci, restart)`:

- Pré-checagem no host: `Get-VMFirmware` (Secure Boot). Se testsigning/kernel_debug exigirem, ofereça `secure_boot_off=true`, que desliga o Secure Boot da VM com a VM Off (Gen2).
- No guest: `bcdedit /set testsigning on`, configuração de debug (serial ou KDNET, ver Fase 5), dump (`CrashDumpEnabled` = kernel ou completo, `AutoReboot`, `Overwrite`, tamanho do pagefile suficiente), HVCI/VBS off se pedido.
- Tudo registrado no `guest-config.json` e revertível.

### 5.3 Execução automatizada

- `lab_run(vm_name, package_id, run_id, expected_manifest_sha256, timeout_seconds, checkpoint_before=true)`: cria checkpoint `pre-<run_id>`, verifica o pacote, executa `Invoke-DriverLabRun.ps1` gerando a frase de aprovação internamente (só no perfil lab), acompanha até término, timeout ou perda do guest.
- Detecção de bugcheck/hang: guest some do PS Direct/SSH + `vm_get` mostra reinício ou heartbeat parado → marca `outcome: "bugcheck" | "hang" | "timeout" | "ok"`. Se o KD estiver anexado (Fase 5), captura `!analyze -v` automaticamente.
- `lab_cycle(...)`: `lab_run` → coleta de evidências (incluindo `MEMORY.DMP`/minidump se houver) → opcionalmente `checkpoint_restore` para `pre-<run_id>` (`revert_after=true`). Retorna um relatório estruturado (outcome, tempos, bugcheck code/params se houver, top frames, caminhos das evidências no host).
- Hang: ferramenta `vm_inject_nmi` (`Debug-VM -InjectNonMaskableInterrupt`) para forçar bugcheck/quebra no debugger quando o guest travar.

### 5.4 Visão e console para a IA

- Screenshot sem VMConnect: implemente `console_screenshot` via `Msvm_VirtualSystemManagementService.GetVirtualSystemThumbnailImage` pedindo exatamente a resolução do video head (de `console_info`). Converta RGB565 → PNG. Assim as coordenadas da imagem batem 1:1 com `console_mouse_move` e funciona sem janela, sem sessão interativa. Mantenha o `PrintWindow` do VMConnect como `source="vmconnect"` opcional. Metadata deve dizer `coordinates_match_mouse: true` quando aplicável.
- `console_wait_for_change(vm_name, timeout_ms, region?)`: espera a tela mudar (hash de imagem reduzida), útil para a IA não fazer polling.
- Cache dos dispositivos `Msvm_Keyboard`/`Msvm_SyntheticMouse` por VM Id no worker, e `console_batch` enviado ao bridge numa única requisição.

## 6. Fase 5 — MCP de WinDbg/KD

### 6.1 Decisão de arquitetura (registre no PROJECT.md)

Existe um projeto MIT maduro, `mcp-windbg` (svnscha, PyPI `mcp-windbg` 1.6.0), que dirige `cdb.exe`/`kd.exe` headless, tem sessões de kernel ao vivo (KDNET, named pipe, serial), dumps de kernel, timeout por comando com break e ressincronização. Avalie primeiro (leia o código-fonte, licença, como faz o break-in e a detecção de fim de comando). Escolha uma das duas e justifique:

(a) depender dele como biblioteca/serviço e escrever só a camada de integração com Hyper-V; ou
(b) implementar o próprio módulo `src/hyperv_console_mcp/kd/`, reaproveitando ideias com atribuição.

Em ambos os casos, as ferramentas ficam no mesmo servidor MCP (prefixo `kd_`) para a IA orquestrar VM + debugger juntos, com um entry point extra `hyperv-kd-mcp` opcional que exponha só as ferramentas `kd_*`.

### 6.2 Requisitos do motor de debug

- Descobrir `kd.exe`/`cdb.exe` (Windows SDK Debugging Tools x64; caminho configurável `HYPERV_MCP_DEBUGGERS_DIR`). WinDbgX (o WinDbg novo) não serve para automação por stdio.
- Detecção de fim de comando com marcador único (`.echo <uuid>`) e parser de prompt (`kd>`, `0: kd>`), nunca por tempo.
- Máquina de estados explícita: `disconnected → waiting_for_target → running → broken → (running|disconnected)`, exposta em `kd_status`.
- Break-in confiável: verifique na prática qual mecanismo funciona com stdio redirecionado (Ctrl+Break via `CREATE_NEW_PROCESS_GROUP` + `GenerateConsoleCtrlEvent`, ou API DbgEng `IDebugControl::SetInterrupt`). Teste live obrigatório. Fallback: `vm_inject_nmi`.
- Saída truncada com limite configurável e marcação de truncamento; log completo de cada sessão em `%LOCALAPPDATA%\HyperVConsoleMCP\kd\<vm>\<timestamp>.log`.
- Símbolos: `_NT_SYMBOL_PATH=srv*<cache local>*https://msdl.microsoft.com/download/symbols` + o diretório do pacote staged (PDB do driver de teste). Cache em `%LOCALAPPDATA%\HyperVConsoleMCP\symbols`.

### 6.3 Transporte de debug

- Serial (sem rede): usa o COM1/named pipe já configurado por `debug_serial_prepare`. O pipe não pode ser compartilhado: `kd_attach` deve desanexar automaticamente o leitor de `serial_connect` (e avisar no resultado), e vice-versa.
- KDNET (rápido): `debug_kdnet_prepare(vm_name, confirm_vm)` usa o switch interno da Fase 2, escolhe porta 50000–50039, gera a chave, roda `bcdedit /dbgsettings net hostip:<ip> port:<p> key:<k>` (ou `kdnet.exe`) no guest via PS Direct, cria regra de firewall no host só para `kd.exe` naquela porta UDP e naquela interface, e grava a chave no Credential Manager. A chave nunca volta em resultado de ferramenta.
- Recomende KDNET quando houver switch interno (dumps completos por serial a 115200 são impraticáveis) e serial quando a VM precisar ficar sem rede alguma.

### 6.4 Ferramentas

`kd_attach(vm_name, transport="serial"|"kdnet", break_on_connect=false, initial_break=false)`, `kd_status`, `kd_break`, `kd_go`, `kd_command(command, timeout_ms)`, `kd_wait_event(timeout_ms)` (bloqueia até break/bugcheck/módulo carregado), `kd_set_breakpoint(expression, kind="bp"|"bu"|"ba")`, `kd_clear_breakpoints`, `kd_break_on_load(module)` (`sxe ld:<module>`), `kd_stack`, `kd_modules(filter)`, `kd_analyze` (roda `!analyze -v` e devolve estrutura: bugcheck code, parâmetros, módulo/driver culpado, `FAILURE_BUCKET_ID`, top frames), `kd_dump_open(path)` (dumps coletados pelo lab, com `kd -z`), `kd_detach`.

Anotações corretas: `kd_command` é `destructiveHint=true` (comandos podem escrever memória/registradores); `kd_analyze`, `kd_stack`, `kd_modules`, `kd_status` são read-only no sentido de não mudar o alvo.

### 6.5 Integração com o ciclo de vida

`lab_cycle(..., debugger="none"|"serial"|"kdnet")`: anexa o KD antes de ligar/rodar, arma `sxe ld:<driver>` se o manifesto indicar o `.sys`, e em bugcheck roda `kd_analyze` automaticamente e anexa ao relatório. `kd_*` respeitam allowlist e `ALLOW_DEBUGGER`; no perfil lab, sem aprovação.

## 7. Fase 6 — Testes, CI e documentação

- Testes unitários com fakes para: seleção de transporte (auto com/sem SSH), parser de prompt do KD, máquina de estados, parser do `!analyze -v` (use saídas reais salvas como fixtures), conversão RGB565→PNG, geração de summary de aprovação sem segredos, revert do `guest-config.json`.
- Testes Windows-only com fakes de processo: um `FakeKd.py` que imita prompts/saídas do `kd.exe` para testar o driver de sessão sem SDK instalado.
- Testes live opcionais (`tests/live_*.py`), guiados por `HYPERV_MCP_LIVE_TEST_VM`: PS Direct persistente, provisionamento SSH, autologon + screenshot, KDNET attach/break/go, `lab_cycle` com um pacote de teste inofensivo (um `Run-Lab.ps1` que só escreve um arquivo).
- CI: Windows (3.11/3.12/3.13) com pytest, ruff, PSScriptAnalyzer, sintaxe PS, smoke MCP stdio contando as ferramentas; Linux só import/unit.
- Docs: `README.md` (tabela de ferramentas atualizada, perfis safe/lab), `docs/SETUP.md` (Credential Manager, perfil lab, switch interno), `docs/SSH.md`, `docs/GUEST-ACCESS.md` (autologon e toggles, como reverter), `docs/WINDBG.md` (serial × KDNET, símbolos, fluxo de bugcheck), `SECURITY.md` (modelo de ameaça atualizado: o portão de aprovação não protege contra um agente que tenha shell no host; o perfil lab dá à IA controle total das VMs allowlistadas).

## 8. Definição de pronto

- [x] Todos os itens da Fase 1 corrigidos, cada um com teste de regressão.
- [ ] `pytest`, `ruff`, PSScriptAnalyzer e smoke MCP verdes localmente no Windows; testes Windows-only pulados limpo no Linux.
- [ ] `HYPERV_MCP_GUEST_TRANSPORT=psdirect` e `=ssh` produzem resultados equivalentes no mesmo conjunto de testes de transporte.
- [ ] Na VM de teste: após `guest_autologon_configure` + reinício, `console_screenshot` (thumbnail) mostra a área de trabalho sem interação humana.
- [ ] `lab_cycle` com o pacote inofensivo roda de ponta a ponta sem cliques, com checkpoint, evidências e revert.
- [ ] `kd_attach` (serial e KDNET) → `kd_break` → `kd_stack` → `kd_go` funcionando live; `kd_dump_open` + `kd_analyze` funcionando num dump real.
- [x] Nenhum segredo (senha, chave SSH privada, chave KDNET) aparece em resultado, evento, auditoria ou log — teste automatizado que procura os valores de teste em toda a saída.
- [x] `docs/PROJECT.md` diz com honestidade o que foi validado live e o que não foi.

Estado em 2026-10-29: código e testes de todas as seis fases estão no branch `v0.2-autonomous-lab`, com 332 testes verdes, Ruff limpo, todos os `.ps1`/`.psm1` parseando e PSScriptAnalyzer sem erros nem avisos. As caixas que continuam abertas exigem uma VM descartável e uma sessão elevada, que não existiam nesta entrega — são exatamente os itens que os scripts `tests/live_*.py` cobrem.

## 9. Como reportar

Ao terminar cada fase, responda com: o que mudou (arquivos principais), testes adicionados, o que foi validado live, o que ficou pendente e por quê. Se alguma premissa deste documento se mostrar errada no Windows real (ex.: comportamento de senha em branco, mecanismo de break-in), corrija a implementação, documente e siga — não force a premissa.
