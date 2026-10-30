# Prompt — Interface única: terminais dentro da GUI, um comando abre tudo (hyperv-console-mcp v0.4)

> Substitui a versão anterior deste arquivo (a TUI isolada). Complementa os prompts v0.3 (`-v0.3`, `-prior-art`, `-dashboard`, `-install`). Novo branch `v0.4`. Mesmas regras: `pytest`/`ruff`/`PSScriptAnalyzer` verdes, teste de regressão por correção, `skipif` no Windows-only, validação live só com `HYPERV_MCP_LIVE_TEST_VM`, changelog honesto em `docs/PROJECT.md`. Assume **host local** (MCP e serviço na mesma máquina que o Hyper-V).

## 0. Objetivo

Uma interface visual única — uma web app (HTML/CSS/JS) com **terminais de verdade embutidos** (xterm.js) num layout de IDE — que é a forma principal de operar e de acompanhar tudo ao vivo. Ela:
- abre com **um comando**, que sobe o serviço, a interface e deixa tudo pronto;
- tem uma tela de **login/credencial** e uma **matriz de permissões por VM**;
- **salva as configurações** (VMs, nível, baseline, transporte) e as **credenciais** (no cofre do Windows), de modo que numa próxima vez já abre configurada;
- opera sem nenhum prompt interativo em runtime (nada de pedir credencial ou confirmação no PowerShell).

A mesma página é entregue de **duas formas**: janela **WebView2** por padrão (sem porta de rede) e um modo **`--serve` localhost+token** opcional para abrir no navegador do operador. Esta leva **aposenta a TUI isolada** e a dashboard tkinter; o WebView2/serve passa a ser a interface.

## 1. Princípios inegociáveis

1. **A interface é cliente fino do serviço único.** Chama os mesmos métodos que o MCP (via `ServiceProxy`/pipe, `service.host.open_service`, `actor="ui"`). **Nenhuma regra de negócio no front-end.** Política, allowlist, nível por VM, gates e redação de segredo ficam no serviço.
2. **Zero prompt em runtime.** O serviço constrói `New-PSSession -Credential` do Credential Manager e usa `-Confirm:$false`; a interface nunca chama `Get-Credential` nem mostra diálogo do PowerShell. Elevação acontece **uma vez na instalação** (conta em *Hyper-V Administrators*), não por ação.
3. **Segredos nunca no front-end nem no log.** Senha, chave SSH e chave KDNET são digitados uma vez, vão para o Credential Manager, e o stream de log é redigido. O front-end recebe só "tem credencial: sim/não".
4. **Mesmos gates do MCP.** O operador pode mais que a IA, mas destrutivo (reverter/apagar/forçar energia) confirma na própria interface.

## 2. Um comando abre tudo, e a config fica salva

- **`hyperv-console-mcp` sem argumentos abre a interface** (janela WebView2) e **sobe o serviço se não estiver rodando** (`ensure_running()`). Capturado a um pipe (um cliente MCP), continua sendo o servidor MCP — a heurística de stdin que já existe decide. `--serve` abre no modo navegador (seção 6). `--stdio` força o servidor MCP.
- **As configurações persistem** no `access.json` por usuário (VMs escolhidas, nível, baseline, transporte) e as **credenciais no Credential Manager** (`HyperVConsoleMCP/<vm>`), exatamente os arquivos/cofre que `hyperv-access` e o MCP já leem. Reabrir a interface **lê o estado salvo** e já aparece configurada: só mostra o login se faltar credencial de alguma VM selecionada. Nada de reconfigurar do zero, nada em arquivo de texto com segredo.
- **Idempotente:** uma segunda abertura não duplica serviço nem reescreve config; ajusta o que mudou.

## 3. Backend habilitador — fluxo de log estruturado unificado

Pré-requisito da doca de logs. Hoje só há o `EventBus` (eventos de ciclo); worker, `guest_exec`/lab, KD, serial, SSH, WMI e `service.log` escrevem espalhado.
- **Registro estruturado único:** `timestamp, source` (worker|kd|serial|ssh|wmi|lab|service), `vm`, `level`, `correlation_id`, `message`, redigido. Todo subsistema escreve nele; é extensão do `EventBus`, não um segundo barramento.
- **`logs_subscribe(after, filters)`** ao lado de `events_wait`, **lock-free**, com filtro por VM/origem/nível e `correlation_id` para agrupar uma operação. Persistência JSONL diária com retenção. Teste: um `guest_exec` gera linhas correlacionadas, sem segredo, entregues por offset.

## 4. Backend habilitador — streaming de saída do KD

Hoje `debugger.kd_command` é request/response; não há como seguir a saída ao vivo.
- **`kd_output_subscribe(vm, after)`** faz tail do log de sessão que o motor já grava, entregando a saída à medida que chega, desacoplada de quem digitou. `kd_command` segue para enviar comandos. Teste contra `tests/fake_kd.py`: saída não solicitada (um break espontâneo) chega pelo subscribe sem comando correspondente.

## 5. Backend habilitador — bootstrap no serviço, sem script à mão

Absorver a lógica de `Start-NorthLabWorker.ps1`/`Invoke-NorthLab.ps1` (preparar o lab, subir o worker, ligar o pipe) no subsistema que o `ensure_running()` já sobe — sem caminho fixo em `G:\`, sem janela interativa, sem Ctrl+C. O serviço detached é o worker. As ações daqueles scripts (Status, StagePackage, SyncSymbols, Checkpoint, Reboot, LiveTest) viram métodos de serviço não-interativos: `vm_power` com `-Force -Confirm:$false`, `New-PSSession` sempre com `-Credential` do cofre (nunca o prompt de `Credential` que apareceu no console do operador), símbolos pelo `kd/symbols.py`. Passo pessoal que falte vira método novo; nunca chamar o `.ps1` externo.

## 6. A interface (HTML único, terminais embutidos)

HTML/CSS/JS sob `webui/assets/`, servido pelos dois modos abaixo. Sem framework pesado obrigatório; se usar um, fixe versão. **xterm.js** (versão exata fixada) para os terminais.

**Tela de login / desbloqueio e seleção:**
- Se alguma VM selecionada não tem credencial no cofre, a interface abre numa tela de **login**: escolher a(s) VM(s), digitar a credencial de cada uma **uma vez** (vai para o Credential Manager; "testar acesso" valida antes). Várias VMs, cada uma com sua credencial.
- **Matriz de permissões por VM:** nível (`read-only`/`control`/`lab`) e as capacidades que a IA pode usar — uma grade visual de "o que eu permito", gravada no `access.json`. É o "seleciono o que quero e o que dou permissão".
- Com tudo salvo, a próxima abertura pula direto para o workspace.

**Workspace (layout de IDE):**
- **Sidebar esquerda:** frota de VMs com estado, nível, baseline, saúde do transporte e um cadeado por VM (credencial presente). Ação de **abrir Hyper-V Manager / VMConnect** da VM selecionada.
- **Centro: abas e painéis divisíveis**, misturando terminais e telas gráficas —
  - **Terminais (xterm.js), vários ao mesmo tempo, divisíveis em grade:** **KD** (entrada + saída em streaming da seção 4, com painéis de breakpoints/stack/módulos/registradores), **Serial** (ler/enviar), **Guest** (REPL de `guest_exec`).
  - **Gráficas:** Frota, Arquivos (dois painéis host↔guest, criar pasta, copiar nas duas direções, hash, progresso), árvore de Checkpoints (baseline bloqueado), Lab (`lab_plan`/`apply`/`cycle`, receipts, triagem de crash com `!analyze` formatado), e a tela da VM via thumbnail.
- **Doca inferior: Logs** — o stream unificado da seção 3, ao vivo, filtrável por VM/origem/nível, com busca, pausa/scroll-lock e agrupamento por `correlation_id`. É onde se vê a IA e o operador agindo lado a lado.
- **Barra superior:** VM ativa, perfil (safe/lab), estado da credencial.
- **Atualização:** o front-end assina `logs_subscribe`/`events_wait`/`kd_output_subscribe` e empurra para os widgets; nunca faz polling de estado que o evento traz; para quando o painel não está visível.

**Dois modos de entrega da mesma página:**
- **WebView2 (padrão):** janela nativa, a página fala com o Python pela ponte do WebView2 — **sem porta de rede**. É o modo recomendado.
- **`--serve` (opcional, navegador do operador):** servidor **só em `127.0.0.1`**, com **token aleatório obrigatório** gerado na inicialização (abre o navegador em `http://127.0.0.1:<porta>/?token=…`; toda requisição e todo websocket exigem o token), **checagem de `Origin`/`Host`** contra DNS rebinding e **proteção CSRF** nas chamadas que mudam estado. Nunca `0.0.0.0`; sem acesso remoto (para outra máquina, só por túnel SSH ao loopback). As operações continuam atrás do pipe do serviço. Atualizar o `SECURITY.md` para admitir este listener e descrever suas travas.

## 7. Consolidação de UI (fazer e registrar)

- WebView2/serve é a interface. **Remover a `dashboard.py` tkinter** (ou reduzir a um fallback mínimo documentado para terminal sem GUI). **Não** criar/retornar a TUI Textual isolada — a versão anterior deste prompt fica obsoleta. Registrar a decisão em `docs/PROJECT.md`.

## 8. Limite honesto do que o terminal mostra

Os terminais embutidos são de **texto** (KD, serial, guest, logs). A tela **gráfica** interativa da VM (clicar/digitar na imagem) é a aba de thumbnail e o botão VMConnect, não um terminal. Deixe isso claro na interface.

## 9. Testes, CI e docs

- View-models/adaptadores testáveis sem navegador, contra um fake do serviço. **Teste de contrato:** todo método que a interface chama existe no serviço (paridade com o MCP).
- Modo `--serve`: testes de que sem token a requisição é recusada, de `Origin`/`Host` inválidos recusados, e de que o bind é loopback.
- `docs/CONSOLE.md`: o comando único, a persistência de config/credencial, o login, a matriz de permissões, os dois modos de entrega e suas travas, o limite do terminal gráfico. Atualizar README e `SECURITY.md`.

## 10. Definição de pronto

- [ ] `hyperv-console-mcp` sem args abre a interface e sobe o serviço; `--serve` abre no navegador com token/Origin/CSRF; `--stdio` força o MCP.
- [ ] Config (VMs, nível, baseline, transporte) e credenciais persistem; reabrir já vem configurado e só pede login do que falta.
- [ ] Backend: `logs_subscribe` (unificado, redigido, lock-free) e `kd_output_subscribe` (streaming), com testes.
- [ ] Bootstrap dos scripts NORTH absorvido no serviço; nenhum prompt de `Credential`/`Confirmar` em runtime.
- [ ] Interface: login + matriz de permissões por VM; workspace com terminais xterm.js (KD/serial/guest) divisíveis, abas gráficas (frota, arquivos, checkpoints, lab, thumbnail) e a doca de logs ao vivo; abrir Hyper-V/VMConnect.
- [ ] Credencial digitada uma vez por VM, guardada no cofre, reutilizada sempre; várias VMs com credenciais distintas.
- [ ] TUI isolada e tkinter aposentadas; decisão registrada.
- [ ] Nenhum segredo no front-end/log; paridade UI↔MCP por teste de contrato; `--serve` com as travas testadas.
- [ ] `docs/PROJECT.md` diz o que foi validado ao vivo (a página em WebView2 e em `--serve` contra `HYPERV_MCP_LIVE_TEST_VM`) e o que não.

## 11. Como reportar

Por seção: o que mudou, os métodos de serviço adicionados (e as ferramentas MCP correspondentes), testes, o que foi validado ao vivo e o que ficou pendente.