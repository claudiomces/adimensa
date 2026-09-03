# Portal de publicação

Uma página que pergunta o nome da planta, recebe o E57 e roda o processo
inteiro sozinho, mostrando o progresso ao vivo e entregando o link no fim.

Python puro, sem dependências. Roda ao lado do Apache, não dentro dele.

---

## Montagem, uma vez

```
C:\xampp\htdocs\nuvens\
   _portal\              <- os scripts ficam aqui
      portal.py
      publicar.py
      extrair_direto.py
      index.html          <- seu visualizador, o modelo de todos os sites
   _modelo\
      vendor\             <- pannellum.js e pannellum.css
      potree\             <- o release do Potree
   entrada\               <- onde os .e57 ficam
   planta-sul\            <- cada publicação cria uma pasta assim
   planta-norte\
```

Ligar:

```
conda activate pdal
cd C:\xampp\htdocs\nuvens\_portal
python portal.py --raiz ..  --entrada ..\entrada  --modelo ..\_modelo ^
                 --url-base http://localhost/nuvens ^
                 --potree-converter C:\ferramentas\PotreeConverter.exe
```

Abra `http://127.0.0.1:8800`. Deixe essa janela do Anaconda aberta — é ela que
executa as conversões.

---

## Página inicial

`pagina-inicial.html` vai para a raiz, renomeado como `index.html`:

```
C:\xampp\htdocs\nuvens\index.html      <- a vitrine
C:\xampp\htdocs\nuvens\assets\favicon.ico
C:\xampp\htdocs\nuvens\plantas.json    <- criado pelo portal
```

Cuidado para não confundir com o `index.html` de `_portal`, que é o
visualizador. São arquivos diferentes, em pastas diferentes.

Ela lista tudo que já foi publicado, lendo o `plantas.json` que o portal
atualiza ao final de cada publicação: nome como você digitou, número de bolhas,
se tem nuvem, tamanho e data. Acima de seis plantas aparece um campo de filtro.

O botão de publicar aponta para `http://127.0.0.1:8800`. Se o portal não estiver
no ar, o botão se apaga e avisa em vez de levar a uma página morta.

Publicações feitas pela linha de comando não entram no índice — só as que
passam pelo portal. Para incluí-las, publique uma vez pelo portal com o mesmo
nome: as etapas já concluídas são puladas e o registro é criado.

---

## Identidade visual

O portal usa a paleta e a tipografia do adimensa.com.br: papel claro `#E4EAF1`
com malha de prancheta, tinta `#0C2A4D`, azul `#1B6FC0`, ciano `#43B0F1`, e as
fontes Archivo e IBM Plex Mono. As linhas de cota separando as seções são o
mesmo vocabulário de desenho técnico da página institucional.

Para o favicon aparecer, copie o arquivo do repositório:

```
_portal\assets\favicon.ico
```

A pasta `assets/` é servida em `/assets/`, então dá para pôr um logotipo ali
(`.png`, `.svg`, `.jpg`) e referenciá-lo. Sem a pasta, o portal funciona
normalmente — o favicon apenas não carrega.

As fontes vêm do Google Fonts. Sem internet na máquina, o navegador cai nas
fontes do sistema e o layout continua correto.

Note que o **visualizador** (`index.html` de cada planta) segue escuro, de
propósito: fundo claro atrás de nuvem de pontos e panorâmica prejudica a leitura
da geometria. O portal é a face institucional; o visualizador é instrumento.

---

## O que acontece a cada publicação

1. O nome vira uma pasta: *"Planta Sul — Unidade 2"* → `planta-sul-unidade-2`.
   Acentos, espaços e símbolos somem; o resultado aparece na tela antes de você
   confirmar.
2. `vendor/` e `potree/` são copiados do modelo, se ainda não existirem ali.
3. O `publicar.py` roda como subprocesso, e cada linha que ele imprime vira uma
   linha do histórico na tela.
4. A barra avança conforme as etapas `[n/5]`.
5. No fim, o botão abre `http://localhost/nuvens/planta-sul-unidade-2/index.html`.

Fechar a aba não interrompe nada — o trabalho continua no servidor. Reabrir a
mesma URL da tarefa mostra o histórico desde o começo.

---

## As duas formas de entregar o E57

**Arquivo já na máquina** (recomendado). Copie o `.e57` para a pasta de entrada
e escolha na lista. Instantâneo, seja o arquivo de 200 MB ou de 72 GB.

**Envio pelo navegador.** Funciona, com barra de progresso, e o servidor grava
em blocos de 4 MB sem carregar nada na memória. Mas não tem retomada: uma queda
aos 80% recomeça do zero. Use só para arquivos pequenos.

---

## Republicar

Publicar com um nome que já existe pede confirmação. Confirmando, o
`publicar.py` retoma: bolhas já extraídas e octree já gerada são puladas, e o
`config.json` com sua calibração é preservado. Na prática, republicar depois de
trocar o `index.html` leva segundos.

---

## Calibração herdada

Um `config-padrao.json` na pasta `_portal` define de que ponto toda planta nova
começa. Sem ele, `correcao_norte` sairia `0` e cada levantamento precisaria ser
recalibrado do zero — sendo que seus scans vêm todos do mesmo equipamento, com a
mesma convenção.

```json
{
 "correcao_norte": 181,
 "sentido_giro": -1,
 "qualidade": { "forma": "paraboloid", "tamanho_ponto": 1.0, "edl_forca": 0.7 }
}
```

Só o que você escrever ali é substituído; o resto continua vindo dos padrões
embutidos. Quando a publicação usa esse arquivo, o histórico registra
*"calibracao herdada de config-padrao.json"*.

O `deslocamento` é a exceção que continua por planta: ele depende do offset que
o PotreeConverter aplicou naquela nuvem específica, então ajuste no `config.json`
do site, não no padrão.

---

## Ajustes que valem conhecer

**`--host 0.0.0.0`** expõe o portal para a rede local. O padrão é `127.0.0.1`,
só a própria máquina. Não há login nem senha: quem alcança a porta pode publicar
e disparar conversões. Exponha só em rede confiável.

**A pasta `potree/` é copiada em cada site** e ocupa dezenas de MB por planta.
Com muitas plantas, vale trocar a cópia por uma junção do Windows:

```
mklink /J C:\xampp\htdocs\nuvens\planta-sul\potree C:\xampp\htdocs\nuvens\_modelo\potree
```

O portal respeita o que já existe, então uma junção criada antes não é
sobrescrita.

**Uma tarefa por vez é o esperado.** Nada impede disparar duas, mas PDAL e
PotreeConverter competem por disco e memória; duas conversões grandes juntas
demoram mais do que em sequência.

---

## Quando algo falha

O histórico mostra a linha exata onde parou, e as mensagens são as mesmas do
`publicar.py` na linha de comando — inclusive as que dizem onde ele procurou o
`nuvem.laz` ou qual executável não encontrou.

Falhas mais comuns: `PotreeConverter nao encontrado` (passe `--potree-converter`
ao ligar o portal), `PDAL nao encontrado` (o portal precisa ser iniciado com o
ambiente conda ativo) e falta de espaço em disco durante a octree.
