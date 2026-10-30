# Start here

This page is the short version. It assumes you have never run a command in
PowerShell and that you have never opened Hyper-V. If you already know your way
around, [SETUP.md](SETUP.md) is the same information for you.

You are installing two things that come as one: a **window** you can use, and a
**server an AI client can drive**. They are the same program, and they share one
background service — what you see in the window is what the AI is doing.

---

## 1. Check your machine (30 seconds)

Windows 10 or 11 **Pro**, **Enterprise** or **Education**. Windows Home cannot run
Hyper-V at all, and the installer will say so instead of failing halfway.

Open PowerShell — press `Win`, type `powershell`, press Enter — and paste this
line:

```powershell
Get-Service vmms, vmcompute | Select-Object Name, Status
```

Both services `Running` means **yes, this machine can run it**. If one is missing,
Hyper-V is not installed yet; the installer offers to install it and will pick up
again after the restart.

You also need at least one virtual machine in Hyper-V. If you have never made one,
open **Hyper-V Manager** from the Start Menu, choose *New → Virtual Machine*, and
click through the wizard — a Windows ISO is enough. You do not need to configure
anything else: the program reads the VMs that are there.

## 2. Install

Download `HyperVConsoleMCP-Setup-<version>.exe` from the
[releases page](https://github.com/maiconzeraxd/hyperv-console-mcp/releases) and
double-click it.

- It installs **for your user only**, into `%LOCALAPPDATA%\Programs\Hyper-V Console`.
  No administrator password is asked for, and nothing is installed machine-wide.
- It does **not** need Python. The interpreter is inside the download.
- At the end it runs the setup routine once: it finds your AI clients (Claude
  Desktop, Cursor, Codex), writes the entry each one needs, adds a **Hyper-V
  Console** shortcut to the Start Menu, and checks that the service answers.

If you prefer the command line, `winget install maiconzeraxd.HyperVConsoleMCP`
does the same thing once the manifest has been accepted into the winget
repository.

> **"Windows protected your PC"?** Windows shows that blue box for any program it
> has not seen before, and the first releases are unsigned. Choose **More info →
> Run anyway**. A signed release does not show the box; the
> [SmartScreen](#windows-protected-your-pc) section below explains what the box
> means and what it does not.

## 3. Answer three questions

The first time the window opens it asks the three things it cannot guess:

1. **Which virtual machine?** It lists the VMs Hyper-V reports. Pick one.
2. **What may the AI do with it?** `read-only` looks and reads, `control` adds
   power, checkpoints, the console and media, `lab` adds guest access and the
   debugger. You can change it later, per VM, in the window.
3. **What is the guest's password?** Used to reach inside the VM over PowerShell
   Direct. It is stored in your own Windows Credential Manager and never appears
   in the window again, in a log, or in anything the AI can read.

Then the window shows the VM. Click it to open a console you can type into, and
open **Activity** to watch what the AI does while it works — every call it makes
appears there as it happens.

**That is the whole install.** There is no configuration file to edit and no
environment variable to set. If you want the AI client instead of the window, it
was already configured in step 2: restart the client and it will list the
`hyperv` server.

---

## Something is wrong

Each of these names the step that failed, and none of them is a stack trace.

| What you see | What it means | What to do |
|---|---|---|
| *"This PC can't run Hyper-V"* / Windows Home | The edition has no Hyper-V | Nothing to fix — Hyper-V is a Pro/Enterprise feature. |
| The install stopped and printed `Enable-WindowsOptionalFeature …` | Hyper-V is installed but switched off | Run the line it printed, restart, run the installer again. It continues where it stopped. |
| The window opens and the list is empty | Hyper-V has no VMs yet | Make one in Hyper-V Manager, then press **Refresh**. |
| *"The guest refused the password"* | The password is not the guest's current one | Open the VM's **Prepare the guest** page and rotate it — the service changes it in the guest and stores the new one. |
| *"Not answering: the service is not running"* | The background service is not up | Close the window and open it again; it starts the service. If it still says so, `%LOCALAPPDATA%\HyperVConsoleMCP\service\service.log` has the reason. |

### Windows protected your PC

The blue box is SmartScreen: Windows has not seen this file before. It is not a
virus warning, and it does not say the program is unsafe — it says Microsoft
cannot vouch for who published it. Releases built without a code-signing
certificate always show it.

**More info → Run anyway** installs normally. The certificate is the only thing
that removes the box, and `packaging/` is built so that a signed release and an
unsigned one are the same build with one extra step.

Antivirus may also quarantine the installer or one of the programs. The window,
the service and the setup tool are unsigned executables that start processes and
control Hyper-V, which is exactly the behaviour heuristics flag. Allow them, or
check the SHA-256 of the download against the release page before running it.

## Removing it

**Settings → Apps → Hyper-V Console → Uninstall**, or the entry in *Add or remove
programs*. It removes the programs, the Start Menu shortcut and the entries it
wrote into your AI clients' config files.

It keeps two things on purpose, because they are yours and not the installer's:
the saved guest password (Credential Manager) and the folder of events, evidence
and lab receipts (`%LOCALAPPDATA%\HyperVConsoleMCP`). To take those too:

```powershell
& "$env:LOCALAPPDATA\Programs\Hyper-V Console\hyperv-setup.exe" --uninstall --remove-credentials --remove-data
```

## Where to go next

- [DASHBOARD.md](DASHBOARD.md) — the window: the fleet, the console, checkpoints,
  the file manager, the lab and the debugger.
- [LAB.md](LAB.md) — running a driver test and collecting evidence.
- [WINDBG.md](WINDBG.md) — the kernel debugger and crash triage.
- [SSH.md](SSH.md) — the alternative guest transport, when PowerShell Direct is not
  available.
- [SETUP.md](SETUP.md) — the client configuration in full, for a machine set up by
  hand.
