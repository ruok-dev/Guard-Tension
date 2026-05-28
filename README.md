# ⚡ GuardaTensão

Monitor de energia com interface TUI para Linux — feito para quem sofre com quedas e oscilações de luz e usa notebook na tomada.

```
  ▄▄  ▄  ▄  ▄▄  ▄▄  ▄▄  ▄▄      ▄▄  ▄▄  ▄▄  ▄▄ ▄  ▄  ▄▄  ▄▄
 █    █  █ █  █ █   █  █ █  █    █   █   █  █ █ █  █ █  █ █
 █ ▄▄ █  █ █▄▄█ █▄▄ █  █ █▄▄█    █▄▄ █▄▄ █  █ █  ▀▀  ██▄█ █▄▄
 █  █ █  █ █  █ █   █  █ █  █    █   █   █  █ █   █  █  █ █
  ▀▀   ▀▀  █  █ ▀▀   ▀▀  █  █    ▀▀  ▀▀   ▀▀  ▀   █  █  █ ▀▀
```
<img width="1918" height="997" alt="image" src="https://github.com/user-attachments/assets/eafe1d72-6814-401c-a397-620e72840a7e" />
---

## O que é

Script Python com interface de terminal (TUI via `curses`) que monitora em tempo real o estado da energia elétrica do seu notebook. Detecta quedas, retornos, oscilações rápidas e instabilidade na rede — e te avisa antes que você perca trabalho.

Interface estilo painel industrial âmbar, com gráficos de histórico, barra de instabilidade e log de eventos ao vivo.

---

## Funcionalidades

### Monitoramento
- Nível de bateria com barra visual em tempo real
- Tensão e corrente de carga (quando disponível pelo hardware)
- Tempo restante de bateria
- Status do AC (tomada online/offline)
- Histórico gráfico de 60 segundos de bateria e corrente

### Detecção de oscilação — 3 métodos
| Método | Como funciona |
|--------|--------------|
| **Ciclo rápido** | Se a luz cair e voltar em menos de 8s, detecta como oscilação imediata |
| **Frequência de eventos** | Conta quedas/retornos nos últimos 5 minutos — 3 ou mais = rede instável |
| **Corrente instável** | Monitora variação de corrente enquanto carrega; saltos de ≥0.15A indicam tensão oscilando |

### Alertas
- Flash na tela + beep sonoro ao detectar queda ou oscilação
- Notificação desktop via `notify-send` (funciona com GNOME, KDE, etc.)
- Aviso de bateria baixa (≤20%) e crítica (≤10%)
- Alerta de queda brusca de bateria (≥5% de uma vez)

### Log
- Log persistente em `~/.guardatensao.log`
- Histórico de eventos visível na própria interface
- Níveis: `START`, `POWER`, `OUTAGE`, `OSCIL`, `WARN`, `CRIT`, `STOP`

---

## Requisitos

- Python 3.8+
- `psutil`
- Terminal com suporte a cores 256 e caracteres Unicode

**Fedora / RHEL:**
```bash
sudo dnf install python3-psutil
```

**Ubuntu / Debian:**
```bash
sudo apt install python3-psutil
# ou
pip install psutil --user
```

---

## Uso

```bash
python3 gt.py
```

### Atalhos de teclado

| Tecla | Ação |
|-------|------|
| `Q` ou `Esc` | Sair |
| `B` | Ligar/desligar beep sonoro |
| `L` | Limpar log na tela |
| `R` | Resetar estatísticas da sessão |

---

## Configuração

As variáveis no topo do arquivo controlam todos os limites:

```python
REFRESH_RATE        = 0.8    # intervalo de atualização em segundos
ALERT_DROP_PCT      = 5      # alerta se bateria cair X% de uma vez
ALERT_LOW_PCT       = 20     # aviso de bateria baixa
CRITICAL_LOW_PCT    = 10     # aviso de bateria crítica
OSCIL_WINDOW_SECS   = 300    # janela de 5min para contar oscilações
OSCIL_EVENT_THRESH  = 3      # N eventos nessa janela = rede instável
OSCIL_RAPID_SECS    = 8      # queda+retorno em X seg = ciclo rápido
CURRENT_VAR_THRESH  = 0.15   # variação de corrente em A para instabilidade
LOG_FILE            = "~/.guardatensao.log"
```

---

## Compatibilidade de sensores

O script lê dados de `/sys/class/power_supply/` além do `psutil`. Tensão e corrente dependem do hardware expor esses valores — notebooks mais antigos ou com controle de bateria proprietário podem não ter todos os dados disponíveis. O monitoramento de plug/unplug funciona em qualquer caso.

---

## Motivação

Desenvolvido para uso em regiões com rede elétrica instável, onde quedas e oscilações frequentes arriscam perda de dados. Roda no terminal sem dependências além do `psutil` — funciona via SSH, tty, tmux ou qualquer emulador de terminal.

---

## Licença

MIT — faça o que quiser.
