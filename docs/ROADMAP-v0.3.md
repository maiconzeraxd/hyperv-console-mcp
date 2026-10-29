# Prompt para o agente de código — hyperv-console-mcp v0.3

Cole este arquivo como instrução para o agente de código aberto na raiz do
repositório hyperv-console-mcp, num host Windows com Hyper-V. Também pode ser
salvo como docs/ROADMAP-v0.3.md.

## 0. Contexto

O projeto é um servidor MCP local (stdio, Python 3.11+, MCP SDK 2.x) que controla
VMs Hyper-V para um laboratório de desenvolvimento e QA de drivers de kernel
Windows, operável por IA, numa VM descartável e allowlistada. A v0.2 entregou:
dois transportes de guest (PowerShell Direct e SSH), autologon e toggles de
automação, perfil lab, lab_run/lab_cycle, screenshot por thumbnail, motor de
kd.exe próprio e 79 ferramentas. Houve validação ao vivo em uma VM de teste
(ciclo do lab, autologon, kd pelo COM1, SSH ponta a ponta).

### Escopo e limites (não alterar)

Este é um testbed de desenvolvimento de drivers assinados / test-signed e de
análise de falhas. Mantém as fronteiras que o projeto já tem, e nenhuma fase
abaixo as enfraquece:

- A ferramenta não fornece, nem automatiza o carregamento de, código de kernel
  sem assinatura por meio de abuso de driver vulnerável (BYOVD) ou de qualquer
  bypass de Driver Signature Enforcement. Carregar um payload continua sendo uma
  ação manual, explícita e separada do operador dentro do guest; o servidor só
  prepara o ambiente, executa um Run-Lab.ps1 que o operador forneceu e coleta
  evidências.
- "Controle total" é só sobre as VMs da allowlist. No host, só são permitidos:
  objetos Hyper-V dessas VMs, o switch interno dedicado, regras de firewall
  restritas a ele, e arquivos sob as raízes configuradas.
- Nunca mudar Secure Boot/HVCI/test-signing no host; só no guest, com registro e
  revert.
- Segredos (senha do guest, chave SSH, chave KDNET) nunca em argumentos,
  resultados, eventos ou auditoria.

## 1. Regras de trabalho

- Branch v0.3. Commits pequenos e temáticos, mensagens em inglês.
- Antes de cada commit: `python -m pytest`, `python -m ruff check src tests
  scripts`, sintaxe de todos os .ps1/.psm1 e PSScriptAnalyzer. Nada vermelho.
- Todo bug corrigido ganha um teste de regressão que falharia antes.
- Testes Windows-only com skipif; a suíte roda limpa em Linux.
- Validação live só com `HYPERV_MCP_LIVE_TEST_VM`, em `tests/live_*.py`, fora do
  pytest padrão.
- Ao fim de cada fase: atualizar `docs/PROJECT.md` (o que foi testado de verdade
  e o que não) e a contagem de ferramentas/estado no README.
- Se uma premissa deste documento se mostrar errada no Windows real, corrija,
  documente e siga — não force a premissa.

## 2. Fase 1 — Correções (primeiro, nesta ordem)

### 2.1 Detecção de bugcheck quebrada por tipo

O bridge emite `uptime_seconds` como `[Math]::Round(...,3)` (float), mas
`lab/run.py` (~linhas 105 e 197) só usa o uptime com `isinstance(x, int)`. Com
float o baseline vira 0 e um crash+reboot é classificado como hang. Aceite
`numbers.Real` (excluindo bool) nos dois pontos. Teste com uptime float.

### 2.2 Momento e fonte da classificação

Ao perder o guest, a VM pode estar ainda gravando o dump (uptime alto, estado
Running), e hoje isso vira hang.

(a) Faça um curto polling do uptime/estado antes de classificar.

(b) Prefira o aviso de crash que o host registra: evento 18590 no log
`Microsoft-Windows-Hyper-V-Worker-Admin` (contém bugcheck code e parâmetros),
lido via `Get-WinEvent` no host — confirme o ID/log no seu host antes de depender
dele. `_bugcheck_events` hoje busca o evento 1001 dentro do guest, que só existe
depois do reboot; mantenha como fallback.

### 2.3 SSH estoura o limite de linha de comando

O transporte SSH envia tudo em `powershell -EncodedCommand ...`; a linha de
comando do Windows tem teto ~32.767 chars. Medido: `guest_exec` falha por volta
de 8 KB de comando, `guest_write_file` por volta de 18 KB, contra os 131.072 que
a API aceita. Envie o script pelo stdin do canal (`powershell -NoProfile
-Command -`) ou suba por SFTP e rode com `-File`. Teste que verifica o tamanho
máximo da linha gerada e um `guest_exec` de 100 KB pelo SSH.

### 2.4 `auto` não recupera quando o SSH cai

A saúde fica em cache 30 s; se o guest reinicia no meio de um `lab_cycle`, as
chamadas vão para o SSH e falham. Em `auto`, numa falha de conexão (comando não
chegou ao guest), invalide o cache e repita por PowerShell Direct. Teste com um
SSH que passa a recusar conexão no meio.

### 2.5 BCD dependente de idioma

`Read-BcdValue`/`Set-BcdValue` em `guest_config.ps1` fazem parsing da saída
traduzida do bcdedit (em pt-BR vem Sim/Não), então status e revert de
testsigning/debug quebram num guest localizado. Troque por WMI `root\wmi`
`BcdStore`/`BcdObject` (IDs numéricos, neutro de idioma), ou, se mantiver
bcdedit, opere por elementos numéricos e não por texto. Varra o restante do
código atrás de qualquer outro parsing de saída localizada (há precedentes já
corrigidos: powercfg, icacls). Teste com saída de bcdedit em pt-BR como fixture.

## 3. Fase 2 — Reduzir a superfície de ferramentas (79 é demais)

Os 79 schemas entram no contexto em toda conversa e aumentam a chance de escolha
errada.

- Toolsets por configuração: `HYPERV_MCP_TOOLSETS=core,console,lab,kd,ssh,media`
  (padrão derivado do perfil). O perfil `safe` não expõe `lab_run`/`lab_cycle`;
  quem não usa debugger não vê as 21 `kd_*`/KDNET.
- Consolidar famílias: as ~10 `console_*` viram uma `console_input` com ação
  tipada (discriminated union) mais `console_screenshot`/`console_wait_for_change`;
  agrupe o breakpoint do kd (`kd_set_breakpoint`/`kd_clear_breakpoints`/
  `kd_break_on_load`) e as ações de execução. Meta: ≤ ~40 ferramentas visíveis
  por perfil, sem perder capacidade.
- Mantenha nomes e anotações (readOnly/destructive) coerentes; atualize o smoke
  que conta ferramentas e a tabela do README.

## 4. Fase 3 — Refatorar service.py

`service.py` tem ~2.100 linhas e ~120 métodos. Separe por domínio (vm, guest,
console, media, lab, kd, ssh), cada um num módulo com sua fatia de testes,
mantendo a fachada pública. Sem mudança de comportamento: a suíte existente deve
passar inalterada antes de adicionar testes novos.

## 5. Fase 4 — Migrar o host para Python/WMI e enxugar o PowerShell

Dos defeitos que só apareceram ao vivo na v0.2, cerca de metade foi
peculiaridade de PowerShell (BOM, PSCustomObject vs hashtable, ExecutionPolicy,
saída localizada, propriedades extras inflando JSON, sshd interpretando o comando
duas vezes). Objetivo: lógica e parsing em Python, PowerShell só onde é
insubstituível.

- Bridge do host → WMI em Python (pywin32/wmi, namespace
  `root\virtualization\v2`): estado da VM, energia, checkpoints
  (`Msvm_VirtualSystemSnapshotService`), DVD/COM (`ModifyResourceSettings`),
  teclado/mouse sintéticos, thumbnail. Trate os jobs assíncronos
  (`Msvm_ConcreteJob`) e traduza os códigos de erro. Faça incrementalmente,
  operação a operação, cada uma com teste, mantendo o worker como fallback até
  migrar.
- Worker PowerShell fino: sobra só o PowerShell Direct (sessão persistente,
  `Copy-Item -ToSession`/`-FromSession`) e cmdlets cujo WMI seja trabalhoso
  demais. Implemente de fato a sessão persistente por VM e o segundo worker de
  guest, que ficaram pendentes na v0.2.
- Guest com PowerShell mínimo: o guest não tem Python; reduza os .ps1 do guest
  ao essencial e mova validação, hashes e orquestração para o Python. Exemplo: o
  guest só lista arquivos com `Get-FileHash` e o Python compara, eliminando a
  classe inteira de bugs de hash canônico divergente.

## 6. Fase 5 — Zero-config, serviço único e nova UI

Objetivo: rodar em qualquer VM do host com o mínimo de configuração; trocar
configuração por descoberta.

- Descoberta: lista de VMs, geração, Secure Boot, COM, DVD vêm do Hyper-V;
  sistema/hostname/IPs do guest vêm do KVP (`Msvm_KvpExchangeComponent`) sem
  credencial; kd.exe/SDK e permissão Hyper-V do host são detectados. Pastas sob
  `%LOCALAPPDATA%\HyperVConsoleMCP\` criadas sozinhas. Remova a necessidade das
  variáveis `ALLOW_*` individuais em favor de um nível por VM
  (read-only/control/lab) escolhido na UI. `--vm NORTH-Test` e `north-kd` deixam
  de ser padrões embutidos.
- Preparação sob demanda: quando uma ferramenta precisa de algo que falta,
  devolve `{code, fix:{tool,args}}` em vez de só um erro; no perfil lab a IA
  corrige sozinha, na UI vira um botão "Corrigir".
- Serviço único: um processo de fundo detém o worker, as sessões de serial e de
  KD, o estado e os eventos. O servidor MCP e a UI são clientes finos desse
  serviço por named pipe protegido (reuse o endurecimento do `ipc.py`:
  `FILE_FLAG_FIRST_PIPE_INSTANCE`, verificação do processo servidor). Isso resolve
  o fato de hoje a UI não enxergar uma sessão de KD/serial aberta pela IA.
- UI responsiva: substitua o tkinter por uma janela WebView2 (ex.: pywebview),
  HTML/CSS responsivo, tema escuro, DPI correto, sem abrir porta de rede.
  Aprovações viram fila lateral não-modal (hoje os diálogos são modais e congelam
  a janela).
- Instalação: `uvx hyperv-console-mcp` e um comando
  `install --client claude|codex|cursor` que escreve a config do cliente MCP e
  cria o atalho da UI.

## 7. Fase 6 — Robustez do testbed de drivers (QA legítimo)

Melhorar a qualidade do laboratório de teste de drivers assinados/test-signed:

- Driver Verifier: ferramenta para ativar/desativar o Verifier
  (`verifier /standard /driver <x.sys>`, flags configuráveis) no guest, com
  registro e revert, e leitura do estado (`verifier /query`).
- Ciclo de vida do test-signing e do serviço de driver: instalar/iniciar/parar/
  remover um serviço de driver de teste (`sc create … type= kernel`,
  `CreateService`/`StartService` via Python no guest por transporte), capturando
  status e NTSTATUS.
- Triagem de crash: enriquecer o relatório do `lab_cycle` com bugcheck code +
  parâmetros + FAILURE_BUCKET_ID + módulo culpado (do `!analyze -v`) e com os
  eventos WHEA/BugCheck do host; anexar o minidump e o log do kd.
- Símbolos/PDB: garantir que o .pdb do driver staged resolve (já previsto no
  `_NT_SYMBOL_PATH`); validar com `lm`/`!lmi` no fluxo e reportar se os símbolos
  do driver de teste carregaram.
- Reprodutibilidade: cada `lab_cycle` produz um relatório estruturado versionado
  (pacote + manifest hash + checkpoint + outcome + evidências) para comparar
  execuções.
- Robustez de interface do driver (opcional): um harness para enviar IOCTLs
  malformados a um driver de teste próprio e observar bugcheck/leak sob
  Verifier — como teste de robustez da sua própria interface, com a VM revertida
  a cada iteração.

## 8. Fase 7 — Testes, CI, docs

- Testes novos para cada item da Fase 1; fakes para WMI (Fase 4) e para o
  serviço único (Fase 5).
- CI: Windows (3.11/3.12/3.13) com pytest, ruff, PSScriptAnalyzer, sintaxe PS,
  smoke MCP contando ferramentas por toolset; Linux só import/unit.
- Teste de segredos (já existente) rodado também contra um build propositalmente
  vazando, como controle negativo.
- Docs: atualizar README (contagem real de testes/ferramentas, toolsets,
  zero-config), SECURITY.md (modelo de ameaça do serviço único e do perfil lab),
  e um `docs/DRIVER-TESTING.md` com o fluxo de QA da Fase 6. Corrigir os números
  defasados ("332 testes", "SSH não exercitado") e o texto da página Lab da UI
  que ainda fala do runner manual.

## 9. Definição de pronto

- [ ] Fase 1 inteira corrigida, cada item com teste de regressão; bugcheck sem
      debugger detectado ao vivo (não como hang).
- [ ] `guest_exec`/`guest_write_file` de 100 KB funcionam pelo SSH e pelo
      PowerShell Direct.
- [ ] `auto` recupera para PowerShell Direct quando o SSH cai no meio de um
      `lab_cycle`.
- [ ] Status e revert de testsigning/debug funcionam num guest pt-BR.
- [ ] ≤ ~40 ferramentas por perfil; smoke atualizado.
- [ ] Operações de host migradas para WMI em Python com fallback testado; sessão
      PowerShell Direct persistente e segundo worker implementados.
- [ ] UI única em WebView2, aprovações não-modais, enxergando sessões de KD/serial
      abertas pela IA.
- [ ] Nenhum segredo em resultado/evento/auditoria/log (teste automatizado).
- [ ] `docs/PROJECT.md` diz com honestidade o que foi validado live e o que não.

## 10. Como reportar

Ao terminar cada fase: o que mudou (arquivos principais), testes adicionados, o
que foi validado live, o que ficou pendente e por quê.
