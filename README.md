# pyDTVTools

O pyDTVTools é um canivete suíço (que expressão clichê) de ferramentas direcionadas ao novo padrão de TV digital que está sendo implementado no Brasil, o DTV+ ou TV 3.0. São algumas ferramentas em linha de comando, desenvolvidas em Python, necessárias para se trabalhar com a captura e manipulação dos arquivos, já que ainda não existem ferramentas populares disponíveis que suportem os conteúdos deste novo padrão de TV adotado aqui.

Caso você seja um uusário que tenha ressalvas ao uso de inteligência artifical na produção de códigos ou na vida em geral (eu acho toda e qualquer imagem gerada por IA horripilante), convido a ler o minha declaração de transparência para entender como isso afetou a produção deste conteúdo.

## Comandos e outras explicações

| Comando | Descrição |
|---|---|
| `ts-tv3` | Padroniza uma captura MPEG-TS -> MP4 com áudio MPEG-H: single-track (LCEVC como SEI T.35) e/ou dual-track (`vvc1` + `lvc1`) |
| `mpegh-scan` | Analisa o áudio MPEG-H: presets, grupos, layout nativo, linha do tempo de disponibilidade |
| `mpegh-audio` | Gera arquivos de áudio dos presets/grupos escolhidos |
| `mp4-cut` | Corte sem perdas (VVC + LCEVC + MPEG-H preservados), com verificação e correção do sincronismo A/V |
| `dash-capture` | Grava o stream DASH ao vivo ou estático (VVC + LCEVC + MPEG-H) em MP4 |
| `transcode` | MP4 single-track -> HEVC ou AV1 10 bits HDR10 (camada LCEVC aplicada) e presets MPEG-H renderizados como trilhas de áudio |
| `frame` | Salva um quadro como imagem HDR (PNG 16 bits, AVIF) e/ou SDR com tone mapping (PNG, JPG) |
| `sdr` | Gera imagens SDR a partir de um PNG HDR salvo |
| `preview` | Extrai quadros reduzidos em SDR (com LCEVC) de alguns instantes: usado na prévia do Corte MP4 |
| `play` | Reproduz um trecho no `ffplay`, sem áudio, com LCEVC e conversão HDR → SDR |
| `fetch-tool` | Baixa um programa (no Windows, o pacote ffmpeg + ffplay + mpegh do `limaalef/ffmpeg-autobuild-lcevc-mpegh`; ou um .zip/.tar informado), confere o SHA-256, mostra a licença e extrai |
| `doctor` | Verifica os programas externos e os módulos Python necessários para o script funcionar |
| `check-update` | Consulta no GitHub se há uma versão mais nova publicada (não baixa nem instala nada) |
| `describe` | Imprime comandos, opções, ferramentas, formatos e layouts em JSON (para interfaces) |

### Opções comuns a todos os comandos

Todos os comandos tem algumas opções compartilhadas e que possuem o mesmo funcionamento. Aqui estão elas:
 
| Opção | Descrição |
|---|---|
| `--mp4box CAMINHO`, `--ffmpeg CAMINHO`, `--mpegh-ui CAMINHO`, `--mpegh-decoder CAMINHO` | Caminho para os programas caso não sejam descobertos automaticamente |
| `--tmpdir PASTA` ou `--tmp-dir PASTA` | Caminho para criação diretório temporário utilizado durante o processamento dos dados |
| `--keep-temp` | Mantém os arquivos intermediários (padrão: remover após uma execução bem-sucedida) |
| `--progress-json` | Uso futuro |
| `-v`, `--verbose` | Mostra os comandos externos executados |
| `--no-color`, `--ascii` | Saída simples: sem cores / sem símbolos Unicode |
| `--overwrite` | Substitui as saídas existentes em vez de usar um nome livre |
 
Códigos de saída: `0` ok, `1` erro inesperado, `2` falha esperada (entrada inválida, programa ausente...), `130` interrompido.
 
O programa não sobrescreve arquivos pré-existentes caso não seja utilizado `--overwrite`: `nome.mp4` vira `nome (1).mp4`, `nome (2).mp4`.
 
Em `mp4-cut`, `-s` e `-e`, e `frame --time` aceitam `HH:MM:SS.mmm`, `MM:SS` ou segundos (`75.5`) (a vírgula também funciona como separador decimal). Já `dash-capture --live-record-limit` utiliza-se de `HH:mm:ss` , `mm:ss` ou segundos.

### `ts-tv3`

Padroniza uma captura MPEG-TS de transmissão da TV 3.0 em arquivos MP4. O vídeo VVC, a camada LCEVC e o áudio MPEG-H são encontrados pelos tipos de stream, pareados e processados pelo MP4Box. FFmpeg não é utilizado neste processo, já que não garante padronização completa do padrão LCEVC, apenas na verificação final.

> [!IMPORTANT]
> Necessário MP4Box para utilizar este comando; FFmpeg opcional.

```
python dtv_tools.py ts-tv3 ENTRADA.ts [--outputs single,dual] [--program N] [--outdir PASTA] [--name NOME] [opções]
```

**Entrada** deve ser um arquivo `.ts` com as trilhas de vídeo VVC e LCEVE, a presença do áudio MPEG-H é opcional.
 
**Saídas:**
 
| Arquivo | Conteúdo |
|---|---|
| `<nome>_single_track_4k.mp4` | Uma trilha de vídeo em codec VVC com a camada LCEVC embutida como SEI T.35 + trilha de áudio MPEG-H |
| `<nome>_dual_track.mp4` | Duas trilhas de vídeo: A base em codec VVC (`vvc1`) e a camada de aprimoramento LCEVC (`lvc1`) + mais a trilha de áudio MPEG-H |
 
| Opção | Descrição |
|---|---|
| `--outputs` | Tipos de saída: `single`, `dual` ou `single,dual`. Padrão: ambos |
| `--program N` | Número do programa a usar. Padrão: o primeiro da PAT (um aviso é impresso quando há vários) |
| `--vvc-pid`, `--lcevc-pid`, `--mpegh-pid` | Força um PID (decimal ou hexadecimal, como `0x100`) quando a detecção automática não encontra corretamente cada trilha |
| `--fps F` | Força uma taxa de quadros constante na entrada do MP4Box. Padrão: medida no stream |
| `--lcevc-size LxA` | Tamanho de quadro gravado na trilha `lvc1` do arquivo dual-track. Padrão `3840x2160` |
| `--outdir PASTA`, `--name NOME` | Pasta de saída e nome-base dos arquivos. Padrão: pasta e nome da entrada |
| `--dry-run` | Só lista programas, PIDs, stream types e descritores sem realizar qualquer processamento |

#### Exemplos

```
# Analisar a captura em MPEG-TS
python dtv_tools.py ts-tv3 captura.ts --dry-run
 
# Salvar padronização do MPEG-TS na versão single-track, em outra pasta, com nome escolhido
python dtv_tools.py ts-tv3 captura.ts --outputs single --outdir gravacao_tv30 --name captura_final
 
# Informando manualmente os PIDs de cada trilha
python dtv_tools.py ts-tv3 captura.ts --vvc-pid 0x100 --lcevc-pid 0x101 --mpegh-pid 0x102
python dtv_tools.py ts-tv3 captura.ts --vvc-pid 256   --lcevc-pid 257   --mpegh-pid 258
```

### `mpegh-scan`
 
Realiza uma análise do áudio MPEG-H e mostra os **presets** e **grupos** (audio elements) existentes para que possam ser escolhidos no uso do comando `mpegh-audio`.

> [!IMPORTANT]
> Necessário MP4Box e `mpeghUiManager` para utilizar este comando.
 
```
python dtv_tools.py mpegh-scan ENTRADA [--native-layout 0|2|6|19] [--cache-dir PASTA] [opções]
```
 
**Entrada:** um MP4 com trilha de áudio MPEG-H ou um arquivo que o MP4Box consiga importar como áudio MPEG-H.

**Saída:** uma lista no console e um `catalog.json` na pasta de cache (`--cache-dir`; padrão
`<pasta temporária do sistema>/dtv_audio_cache`).
 
| Opção | Descrição |
|---|---|
| `--native-layout` | `0` = detectar pelo stream (padrão), `2` = 2.0, `6` = 5.1, `19` = 7.1.4 |
| `--mpegh-ui CAMINHO` | Executável do `mpeghUiManager` |
| `--cache-dir PASTA` | Onde ficam a análise e o áudio extraído |

#### Exemplos

```
python dtv_tools.py mpegh-scan captura_single_track_4k.mp4
```
 
##### Saída (formato):
 
```
Layout detectado no stream: 5.1 (usando 5.1)
Presets encontrados no vídeo:
  --option preset:0   → Preset 0: <nome> [G1 (<nome>), G2 (<nome>)]
  --option preset:1   → Preset 1: <nome> [G1 (<nome>), G3 (<nome>)]
Grupos (Audio Elements) encontrados no vídeo:
  --option group:3    → Grupo 3: <nome> (em P1)
```
 
Os ids (`preset:0`, `group:3`) são o que o `mpegh-audio --option` espera para processar os áudios.

### `mpegh-audio`
 
Decodifica os presets/grupos escolhidos e codifica cada um em um arquivo de áudio; também pode acrescentá-los como trilhas de uma cópia do MP4.

> [!IMPORTANT]
> Necessário FFmpeg, MP4Box, `mpeghDecoder` e `mpeghUiManager` para utilizar este comando.
 
```
python dtv_tools.py mpegh-audio ENTRADA [--option preset:0 --option group:3=flac ...] [--mux none|add|only] [opções]
```
 
| Opção | Descrição |
|---|---|
| `--option ESCOLHA` | Repetível. `preset:ID` ou `group:ID`, opcionalmente com `=FORMATO` e `:BITRATE`: `preset:0`, `preset:0=eac3:640k`, `group:5=flac`. No fim, `@LAYOUT` renderiza a trilha em outro layout (`2.0` ou `5.1`): `group:3@2.0`, `preset:1=eac3:448k@5.1`. Sem `@`, vale o layout original da trilha (o `mpegh-scan` indica qual é e quais outros são possíveis; um par L/R é sempre 2.0). Mudar o layout muda o formato padrão (AAC em 2.0, E-AC-3 em 5.1) e o nome do arquivo, e, no `transcode`, o título da trilha (`2.0 de 5.1`). Sem nenhum `--option`, **todos os presets** são gerados. Os IDs vêm do `mpegh-scan` |
| formatos | `aac` (até 8 canais, 256k), `eac3` (até 6 canais, 640k), `flac` (até 8 canais), `wav` (24 bits). Padrão por opção: AAC com 2 canais, E-AC-3 até 6 canais, WAV acima disso |
| `--native-layout` | `0` automático (padrão), `2`, `6`, `19` |
| `--jobs N` | Decodificações em paralelo. Padrão: metade dos núcleos da CPU |
| `--mux` | `none` (padrão): gera apenas os arquivos de áudio. `add`: gera os arquivos de áudio **e** `<nome>_multiaudio.mp4` com vídeo original e todas as trilhas processadas. `only`: só o MP4, com os arquivos de áudio removidos |
| `--audio-delay MS` | Atraso em ms aplicado às trilhas acrescentadas pelo `--mux`. Padrão = 0 |
| `--normalize off\|ebu\|atsc\|streaming\|LUFS` | Normaliza o volume dos áudios gerados. `ebu` = −23 LUFS, `atsc` = −24, `streaming` = −16, ou um valor entre −40 e −5. Padrão = `off`. O volume integrado e o pico verdadeiro (EBU R128) são medidos no áudio já decodificado e aplica-se **um único ganho** constante, então a dinâmica da mistura não muda. O ganho nunca leva o pico acima de `--true-peak`: se levaria, ele é reduzido, o arquivo fica abaixo da meta e o registro avisa. Não é o mesmo que `--target-loudness`, que usa os metadados do MPEG-H dentro do decoder |
| `--true-peak DBTP` | Pico verdadeiro máximo depois de `--normalize`, entre −9 e 0 dBTP. Padrão = −1 |
| `--drc-effect N` | Tipo de efeito DRC do decoder (`-dse`, padrão 0) |
| `--target-loudness NÍVEL` | Loudness alvo do decoder (`-rl`); vazio = padrão do decoder |
| `--sleep-mode`, `--uim-version` | Avançado: como os comandos de interatividade são temporizados (`relative`) e a versão do protocolo do UI manager (`11.0`) |
| `--outdir PASTA` | Pasta de saída. Padrão: ao lado da entrada |
| `--cache-dir PASTA` | Mesmo cache do `mpegh-scan` |
 
**Nomes de saída:** `<nome da entrada>_Preset_<id>_<nome do preset>_<layout>.<ext>` e
`<nome da entrada>_Grupo_<id>_<nome do grupo>_<layout>.<ext>` (por exemplo `movie_Preset_1_Narrador_5.1.eac3`).

#### Exemplos

```
# Todos os presets, formatos padrão
python dtv_tools.py mpegh-audio captura.mp4 --outdir audio_processados
 
# Preset 1 em E-AC-3 a 448k, grupo 3 em FLAC, quatro decodificadores ao mesmo tempo
python dtv_tools.py mpegh-audio captura.mp4 --option preset:1=eac3:448k --option group:3=flac --jobs 4 --outdir audio_processados
 
# Uma cópia do MP4 com os áudios escolhidos como trilhas (arquivos de áudio avulsos não são mantidos)
python dtv_tools.py mpegh-audio captura.mp4 --option preset:0 --option preset:1 --mux only
```

### `mp4-cut`
 
Corta um MP4 **sem recodificar** em quadros IDR (VVC, LCEVC e MPEG-H ficam como estão). Nesta função, apenas o MP4Box é utilizado no processamento, o FFmpeg só realiza verificação final para confirmar que a camada LCEVC ainda está presente.

> [!IMPORTANT]
> Necessário MP4Box para utilizar este comando; FFmpeg opcional.
 
```
python dtv_tools.py mp4-cut ENTRADA.mp4 -s INÍCIO -e FIM [-s INÍCIO -e FIM ...] [opções]
python dtv_tools.py mp4-cut ENTRADA.mp4 --list-idr
```
 
**Entrada:** arquivo MP4 com trilha de vídeo.

**Saídas:** um MP4 por par `-s/-e`.
 
| Opção | Descrição |
|---|---|
| `-s`, `-e` | Início / fim desejados. Repita o par para vários cortes. Sem `-s`, em um terminal, o comando pergunta de forma interativa |
| `--snap` | Qual IDR usar para o início (e para o fim, se não houver `--end-snap`): `nearest` (padrão, o mais próximo), `prev` (anterior) ou `next` (seguinte) |
| `--end-snap` | O mesmo para o fim, mais `eof` (cortar até o fim do arquivo) |
| `--list-idr` | Só lista os pontos de corte; não grava nada |
| `-o`, `--output` | Arquivo de saída. Com vários cortes, `_01`, `_02`... é acrescentado ao nome |
| `--no-av-fix` | Só mede o sincronismo após o corte; não reescreve a edit list |
| `--overwrite` | Substitui as saídas existentes |
 
Um corte só pode começar em um IDR, portanto o início real é o IDR escolhido por `--snap`, e o fim também é um IDR (ou o fim do arquivo com `--end-snap eof`).
 
**Nomes de saída** (sem `-o`): `<nome>_cut_<início>s_<fim>s.mp4` para um corte e `<nome>_cut_01_<início>s_<fim>s.mp4`... para vários.
 
```
# Listar tempos disponíveis para corte
python dtv_tools.py mp4-cut movie.mp4 --list-idr
#   Duration 00:00:20.000 · 500 frames · 10 cut points (1 every ~2.00s) · types: IDR
 
# Um corte: do IDR mais próximo de 1:15.500 até o mais próximo de 2:30
python dtv_tools.py mp4-cut movie.mp4 -s 00:01:15.500 -e 00:02:30
 
# Um corte: do IDR anterior aos 75,5 segundos de vídeo até o IDR após 150 segundos
python dtv_tools.py mp4-cut movie.mp4 -s 75.5 -e 150 --snap prev --end-snap next
 
# Dois cortes: primeiro entre 2 e 6 segundos e o segundo de 12 segundos até o fim do arquivo, com nome escolhido
python dtv_tools.py mp4-cut movie.mp4 -s 2 -e 6 -s 12 -e 20 --end-snap eof -o cuts/part.mp4
```

### `dash-capture`
 
Grava um stream DASH ao vivo ou estático (base VVC + enhancement LCEVC + áudio MPEG-H) e gera um MP4 (VVC com a camada LCEVC como SEI T.35 + o áudio MPEG-H). Precisa do MP4Box e do módulo Python `requests`; o `rich` dá o painel de status colorido. Use
somente em streams que você tem o direito de gravar; streams criptografados são recusados.

> [!IMPORTANT]
> Necessário MP4Box para utilizar este comando. No Python, é necessário ter os módulos `requests` e `rich` instalados.

> [!WARNING]
> Apenas streams sem criptografia são suportados!
 
```
python dtv_tools.py dash-capture URL [--save-dir PASTA] [--save-name NOME] [--live-record-limit HH:mm:ss] [opções]
```
 
**Entrada:** a URL do manifesto (MPD).

**Saída:** `<save-dir>/<save-name>.mp4` (nome padrão `capture_AAAAMMDD_HHMMSS`). Durante a gravação, uma pasta temporária `<save-name>_tmp` (dentro de `--tmpdir`; padrão `--save-dir`) guarda os segmentos; ela é apagada depois que o MP4 é gerado.
 
| Opção | Descrição |
|---|---|
| `--save-dir PASTA`, `--save-name NOME` | Pasta (padrão onde o script é executado) e nome do arquivo, sem extensão |
| `--thread-count N` | Downloads em paralelo (padrão 8) |
| `--live-record-limit HH:mm:ss` | Encerra gravação ao alcançar este tempo |
| `--live-take-count N` | Começa pelos últimos N segmentos da janela do manifesto. Padrão = 1s |
| `--live-wait-time SEG` | Intervalo de atualização do manifesto. Padrão = 0,4s |
| `--cleanup-after SEG` | A cada SEG segundos apaga segmentos `.m4s` já processados. Padrão = 30s |
| `--del-after-done [BOOL]` | Apaga os temporários depois que o MP4 é gerado. Padrão = true; `false` os mantém, como `--keep-temp` |
| `--log-level` | `OFF`, `ERROR`, `WARN`, `INFO` (padrão) ou `DEBUG` |
| `--header 'Nome: valor'` | Cabeçalho HTTP extra, repetível (por exemplo `--header 'Authorization: Bearer ...'`) |
| `--user-agent TEXTO`, `--proxy URL` | Identidade do cliente HTTP (padrão `dtv_tools/<versão>`) e proxy |
| `--max-errors N` | Para e empacota após N falhas consecutivas do manifesto. padrão = 150, `0` = nunca) |
 
```
# Grava 10 minutos e empacota o arquivo final
python dtv_tools.py dash-capture "https://example.com/live.mpd" --save-dir tv30_gravacao --save-name captura_final --live-record-limit 00:10:00
 
# Manifesto estático (o stream inteiro), 16 downloads em paralelo, por um proxy
python dtv_tools.py dash-capture "https://example.com/vod.mpd" --thread-count 16 --proxy http://127.0.0.1:8080
 
# Mantém os segmentos para inspeção, log mais silencioso
python dtv_tools.py dash-capture "https://example.com/live.mpd" --keep-temp --log-level WARN
```
 
Pare uma gravação ao vivo com **Ctrl+C** (ou SIGTERM / Ctrl+Break): ela para de baixar e ainda empacota o que foi gravado.

### `transcode`
 
Recodifica o MP4 single-track (VVC c/ LCEVC SEI + MPEG-H) para **HEVC ou AV1 10 bits HDR10** (x265, NVENC, SVT-AV1 ou NVENC AV1), aplicando o enhancement LCEVC pelo filtro `lcevc` do FFmpeg. O mastering display e o MaxCLL/MaxFALL são lidos do primeiro quadro;
se o stream não tiver, usa 1000/0,005 nits e 1000/300 e avisa em uma nota.

> [!IMPORTANT]
> Necessário FFmpeg, `mpeghDecoder` e `mpeghUiManager` para utilizar este comando. No Python, é necessário ter os módulos `numpy` e `Pillow` instalados.
 
```
python dtv_tools.py transcode ENTRADA.mp4 [-o SAIDA.mp4] [--audio render|copy|none] [--crf 18] [opções]
```
 
| Opção | Significado |
|---|---|
| `-o`, `--output` | Saída (`.mp4`, `.m4v`, `.mov` ou `.mkv`). Padrão `<nome>_hevc_hdr10.mp4` ou `<nome>_av1_hdr10.mp4` (`.mkv` quando há trilhas PCM/FLAC) |
| `--audio` | `render` (padrão): cada preset MPEG-H vira uma trilha, como em `mpegh-audio`. `copy`: só a trilha original (não precisa das ferramentas MPEG-H); `none` |
| `--option`, `--audio-format`, `--audio-delay`, `--normalize`, `--true-peak`, `--original-audio`, `--native-layout`, `--jobs` | Mesmo significado de `mpegh-audio`; `--original-audio keep` acrescenta a trilha original MPEG-H no fim |
| `--container` | `auto`, `mp4` ou `mkv` |
| `--crf`, `--preset`, `--encoder-params` | Ajustes do encoder. Os padrões e as escalas dependem do codificador (veja abaixo). Parâmetros extras são anexados aos do HDR10. `--x265-params` e `--nvenc-params` são nomes antigos de `--encoder-params` |
| `--max-bitrate MBPS` | Limita o **pico** de bitrate do vídeo, em Mbit/s, mantendo a qualidade de `--crf` (0 = sem limite, o padrão). Funciona nos quatro codificadores (tabela abaixo). Referência: HEVC 20 Mbps, AV1 15 Mbps. O limite é aproximado: a média de um trecho curto pode passar um pouco dele |
| `--encoder` | `x265` (CPU, padrão), `nvenc` (HEVC na GPU NVIDIA), `av1` (SVT-AV1, CPU) ou `av1-nvenc` (AV1 na GPU NVIDIA RTX 40 ou mais nova). Veja a tabela abaixo |
| `--no-lcevc` | A origem não tem LCEVC (arquivo HDR10 comum): não usa o filtro |
| `--size WxH`, `--no-size-fix` | Tamanho da saída. Por padrão só se usa um tamanho medido pela verificação LCEVC |
| `--verify-frames` | Quadros decodificados na verificação LCEVC (padrão 48) |
| `--limit SEG` | Codifica só os primeiros SEG segundos (teste rápido) |
| `--dry-run` | Mostra o comando do ffmpeg e para |
 
O arquivo é gravado como `<saída>.part` e só recebe o nome final quando está completo; uma execução que falha nunca deixa um arquivo truncado com o nome final. **Extras do `result`:** `rows`, `notes`, `tracks`. **Etapas:** probe, (decode), encode, report.
 
### `frame`
 
Salva um quadro do mesmo tipo de arquivo como imagem: HDR (PNG 16 bits, AVIF 10 bits) e/ou SDR com tone mapping (PNG, JPG).

| `--encoder` | Codificador do ffmpeg | `--crf` padrão (faixa) | `--preset` (padrão) | Extras em `--encoder-params` |
|---|---|---|---|---|
| `x265` | `libx265` | 18 (0–51) | `ultrafast`…`placebo` (o do x265) | `chave=valor:chave=valor` |
| `nvenc` | `hevc_nvenc` | 22 (0–51), valor CQ | `p1`…`p7` (`p5`) | `-opção valor …` |
| `av1` | `libsvtav1` | 28 (0–63) | `0`…`13` (`6`) | `chave=valor:chave=valor` |
| `av1-nvenc` | `av1_nvenc` | 28 (0–63), valor CQ | `p1`…`p7` (`p5`) | `-opção valor …` |

Como `--max-bitrate` chega a cada codificador: x265 usa `vbv-maxrate` e `vbv-bufsize` (CRF limitado); SVT-AV1 usa `mbr` (só no modo CRF); os NVENC usam `-maxrate` e `-bufsize` com `-rc vbr -cq … -b:v 0`. O buffer é sempre o dobro do limite. Testado com x265 e SVT-AV1 em codificações reais; nos NVENC o comando é o recomendado pela NVIDIA, mas não foi testado sem GPU.

Os valores de qualidade não são equivalentes entre codificadores. No AV1, o mastering display e o MaxCLL/MaxFALL vão pelos parâmetros do SVT-AV1 (precisa da versão 1.2 ou mais nova). Nos codificadores NVENC eles só saem no arquivo se vierem nos quadros de entrada: confira a saída com `ffprobe -show_frames`. AV1 não é aceito em `.mov`. O `doctor` mostra quais codificadores o seu ffmpeg tem, e o `transcode` recusa, antes de começar, um que ele não tenha.

> [!IMPORTANT]
> Necessário FFmpeg para utilizar este comando. No Python, é necessário ter os módulos `numpy` e `Pillow` instalados.
 
```
python dtv_tools.py frame ENTRADA.mp4 --time 00:01:23.5 [--hdr png,avif] [--sdr png,jpg] [opções]
python dtv_tools.py frame ENTRADA.mp4 --frame 2500
```
 
| Opção | Significado |
|---|---|
| `--time` / `--frame` | O quadro, por tempo (quadro mais próximo; uma nota avisa se o tempo está fora do vídeo) ou por número na ordem de exibição (a partir de 1) |
| `--hdr` | `png` (padrão; 16 bits, BT.2020/PQ com `cICP`, `mDCV` e `cLLI`), `avif`, lista separada por vírgula ou `none` |
| `--sdr` | `png` (padrão), `jpg`, lista ou `none` |
| `--avif-crf`, `--avif-chroma` | Qualidade e croma do AVIF (`444` padrão; `420` é o mais compatível). Precisa de `libaom-av1`. **O AVIF não leva mastering/MaxCLL; o PNG leva** |
| `--tonemap` | `mobius` (padrão), `hable`, `reinhard`, `clip`; com `--white-nits`, `--peak-nits`, `--exposure`, `--desat`, `--saturation` |
| `--jpg-quality` | 1-95 (padrão 95) |
| `--slow-seek` | Decodifica desde o início do arquivo em vez do IDR mais próximo |
| `-o`, `--output`, `--outdir` | Nome base (`_hdr.png`, `_sdr.png`... são acrescentados) e pasta |
 
`--no-lcevc`, `--size` e `--no-size-fix` funcionam como em `transcode`.
 
### `sdr`
 
Gera imagens SDR (PNG/JPG, BT.709/sRGB, ICC embutido) a partir de um PNG HDR de 16 bits criado por `frame`, para testar outros ajustes de tone mapping sem decodificar o vídeo de novo.

> [!IMPORTANT]
> No Python, é necessário ter os módulos `numpy` e `Pillow` instalados.
 
```
python dtv_tools.py sdr ENTRADA_hdr.png [--sdr png,jpg] [--tonemap hable] [--white-nits 203] [-o NOME]
```
 
Mesmas opções de tone mapping de `frame`.

### `check-update`

Consulta a última *release* publicada no GitHub e compara com a versão instalada. Não baixa nem instala nada: mostra a versão, as notas, a página e o arquivo que parece ser o do seu sistema.

```
python dtv_tools.py check-update [--repo DONO/NOME] [--timeout SEG]
```

O repositório padrão fica em `UPDATE_REPO` no início da seção de atualização do script e pode ser trocado por `--repo` ou pela variável `DTV_UPDATE_REPO`. Só considera *releases* estáveis (rascunhos e pré-lançamentos publicados como tal ficam de fora). Sem internet, repositório inexistente ou limite de consultas do GitHub viram uma mensagem clara e código de saída 2. Na interface: **Ajuda → Verificar atualizações…**, e uma verificação silenciosa ao abrir (no máximo uma por dia, desligável em **Ajuda → Verificar atualizações ao iniciar**; "Ignorar esta versão" silencia o aviso daquela versão).

### `doctor`
 
Verifica os programas externos, os módulos Python e diz onde cada um foi encontrado.
 
```
python dtv_tools.py doctor [--mp4box CAMINHO] [--ffmpeg CAMINHO] [--mpegh-ui CAMINHO] [--mpegh-decoder CAMINHO]
```

**Saída:**
```
  OK MP4Box: /usr/bin/MP4Box  [PATH]  MP4Box - GPAC version 2.2.1
  OK ffmpeg: /usr/local/bin/ffmpeg  [PATH]  ffmpeg version 6.1.1
  !   ffmpeg has no 'lcevc' filter: the LCEVC enhancement check will be skipped.
  ! mpeghUiManager: NOT FOUND (Searched: --mpegh-ui option, dtv_tools.json, PATH, /path/to/dtv/tools)
  ! mpeghDecoder: NOT FOUND (...)
  OK Python module 'requests' (dash-capture): ok
  OK Python module 'rich' (dash-capture live panel): ok
  OK Python module 'numpy' (frame, sdr): ok
  OK Python module 'PIL' (frame, sdr (SDR images)): ok
  OK all required programs found
```
 
`[PATH]` é a origem do achado: `option`, `dtv_tools.json`, `PATH` ou `tools`. Código de saída `0` quando o programa obrigatório
(MP4Box) é encontrado, `2` caso contrário. O evento `result.tools[]` traz `key, name, ok, path, source, version, caps, needed_by`.
## Programas externos

| Programa | Necessário para |
|---|---|
| MP4Box (GPAC 2.x) | `ts-tv3`, `mp4-cut`, `dash-capture`, `mpegh-scan`, `mpegh-audio` |
| ffmpeg | `mpegh-audio`, `transcode`, `frame` (`transcode` precisa do codificador escolhido: libx265, hevc_nvenc, libsvtav1 ou av1_nvenc; `frame --hdr avif` de libaom-av1); verificação opcional do LCEVC nos demais. O filtro `lcevc` estará disponível apenas em builds personalizadas compiladas com LCEVC |
| mpeghUiManager, mpeghDecoder | `mpegh-scan`, `mpegh-audio`, `transcode --audio render` |
| ffplay (opcional) | `play` (reprodução do trecho na prévia do Corte MP4). Use o da mesma build do ffmpeg, com VVC e LCEVC; é procurado também ao lado do ffmpeg |
 
Módulos Python, importados só quando necessários: `requests` (`dash-capture`), `rich` (painel ao vivo do `dash-capture`), `numpy` e `Pillow` (`frame` e `sdr`);.
 
Eles **não estão incluídos**. Eles podem ser disponibilizados para utilização no script das seguintes formas:
 
1. por meio da opção de linha de comando (`--mp4box`, `--ffmpeg`, `--mpegh-ui`, `--mpegh-decoder`);
2. `dtv_tools.json` ao lado do script (veja `dtv_tools.example.json`);
3. `PATH`, caminho salvo como variável do ambiente;
4. a pasta `tools/` ao lado do script (e suas subpastas diretas)

## Interface gráfica e protocolo para máquinas

A pasta `gui/` tem uma interface desktop nativa (Qt 6 / PySide6) que executa estes mesmos comandos. Quando uma fila de tarefas termina (com sucesso,
com erro ou interrompida), ela mostra um **aviso nativo do sistema** (central de notificações do Windows, Central de Notificações do macOS ou o
serviço de notificações do desktop no Linux); clicar no aviso traz a janela para a frente. Em **Exibir → Aviso ao terminar** dá para desligá-lo,
limitá-lo a quando a janela não está em foco (o padrão) ou enviar um aviso de teste. Análises e a verificação silenciosa do início não avisam.

Para qualquer programa que controle o script, `--json` imprime **somente** eventos JSON Lines no stdout (`hello`, `begin` com as
etapas e seus pesos, `progress` com a etapa e sua fração, `log`, `result`, `error`), `--control` permite parar a execução pelo
stdin e `describe` imprime comandos, opções, ferramentas, formatos e layouts em JSON. Os campos só são acrescentados.

### Gerar o executável e assiná-lo

`python gui/packaging/build.py` gera o executável (arquivo único no Windows e no Linux, `.app` no macOS), testa e empacota em `release/`. O
workflow do GitHub faz o mesmo nos quatro sistemas e, numa tag `v*`, publica a Release. **A assinatura é opcional**: sem certificado, o
executável sai sem assinatura (o Windows mostra o aviso do SmartScreen e o macOS pede para liberar o app).

| Sistema | O que é preciso | Onde configurar no GitHub (Settings → Secrets and variables → Actions) |
|---|---|---|
| Windows | Certificado de assinatura de código (`.pfx`) de uma autoridade certificadora | Segredos `WINDOWS_CERT_PFX_BASE64` (o `.pfx` em base64: `base64 -w0 cert.pfx`) e `WINDOWS_CERT_PASSWORD` |
| macOS | Conta Apple Developer (paga) e certificado *Developer ID Application* exportado como `.p12` | Segredos `MACOS_CERT_P12_BASE64`, `MACOS_CERT_PASSWORD`, `MACOS_NOTARY_APPLE_ID`, `MACOS_NOTARY_TEAM_ID`, `MACOS_NOTARY_PASSWORD` (senha específica de app); variável `MACOS_SIGN_IDENTITY` (`Developer ID Application: Nome (TIMEID)`) |
| Linux | Nada: não há assinatura de executável | — |

O macOS é assinado com Hardened Runtime, o `.dmg` é notarizado pela Apple e o resultado é grampeado (`stapler`). No Windows, o executável é
assinado com SHA-256 e carimbo de tempo (`DTV_SIGN_TIMESTAMP` troca o servidor) e a assinatura é verificada. Para outra ferramenta de assinatura
(Azure Trusted Signing, SignPath, token de hardware), defina `DTV_SIGN_COMMAND` com o comando, usando `{file}` no lugar do arquivo.
A variável `REQUIRE_SIGNING = true` faz as Releases do Windows e do macOS falharem se faltar o certificado. Em tags, o workflow também gera um
atestado de procedência do GitHub para cada arquivo (`gh attestation verify ARQUIVO --repo DONO/REPOSITORIO`). A assinatura é feita antes do teste
de fumaça, então um executável que parou de abrir por causa dela é pego no próprio build.

## Aviso legal
 
- **Software de terceiros.** As ferramentas MPEG-H (Fraunhofer) e LCEVC (V-Nova), o MP4Box (GPAC) e o ffmpeg têm licenças e termos de patente próprios. Este projeto não os contém nem os redistribui; você os instala por conta própria e é responsável por cumprir os termos deles.
- **Captura de streams.** `dash-capture` e `ts-tv3` não contorna nenhuma proteção e recusa streams com DRM.
- O software é fornecido "no estado em que se encontra", sem garantia.

## Declaração de transparência

Eu entendo que a inteligência artificial usada em substituição aos profissionais qualificados ainda é algo totalmente ineficiente a curto ou a longo prazo. Por isso, escrevo este trecho para dizer, a quem interessar, que esta ferramenta foi criada com imenso auxilio do Google Gemini (Argon 4) e Claude AI (Sonnet 5.5).

Sempre que vou fazer algo, parto para buscar o máximo de informações que possa encontrar, mas, como neste caso, confesso que não achei o que precisava. Iniciei a busca a partir do padrão de TV adotado na TV 3.0, uma variação do ATSC 3.0, mas as informações são parcas, principalmente as técnicas. Então, conversando com outros curiosos e ainda pesquisando, em cerca de 2 meses não consegui chegar em nada que fosse útil.

Assim sendo, parti para o uso do Google Gemini para ver se extraia algo de útil e, para minha surpresa, foi algo bem-sucedido. Se a explicação entregue é algo que faz sentido, que é real, eu juro que não sei, mas os códigos ali gerados de fato geraram resultados satisfatórios, que foram úteis e me permitiram, pela primeira vez, manipular os arquivos para gerar o resultado final que esse padrão de codec adotado aqui exige. Por isso, se você é um especialista, um curioso que entende um pouco disso ou só quer validar o que foi gerado, vou deixar aqui o [PDF dos prompts iniciais e das respostas geradas](https://drive.google.com/file/d/1pjHcOyvzXk_3eIZPEQ-PkpxYZwex5B7m/view?usp=sharing) para que qualquer um possa analisar e até, talvez, aprender.

E caso você chegue a conclusão que o código é horroroso, ineficiente e/ou tem coisa melhor por aí, fique a vontade para reclamar comigo, para melhorar ou para compartilhar qualquer descoberta ou criação sua, que eu vou ter o prazer de ler e testar. Pode me chamar no [Bluesky](https://bsky.app/profile/limaalef.com).
