# Med Escolha — Static Ad Generator

Pipeline de geração de ads estáticos para Med Escolha usando kie.ai (Nano Banana 2 + Nano Banana 1 edit).

## Estrutura

```
ads/med-escolha/
├── README.md                 ← este arquivo
├── gallery.html              ← visualizador de todas as imagens geradas
├── singles/                  ← 31 templates × 2 versões = 62 peças individuais
│   ├── 01-headline-manifesto/
│   ├── 02-anti-claim/
│   └── ... (31 pastas no total)
├── carousels/                ← 2 carrosséis prontos pra subir no Meta
│   ├── README.md             ← mapa dos slides
│   ├── carousel-10-3-sinais/
│   └── carousel-25-3-verdades/
├── videos/                   ← 18 vídeos 8s gerados via Veo3 Fast
│   ├── README.md             ← mapa dos vídeos
│   ├── motion/               ← 10 motion graphics standalone
│   ├── ugc-creative-01/      ← 4 clips do script de Mariana
│   └── ugc-creative-02/      ← 4 clips do script de Pedro
├── copy/                     ← scripts de vídeo + prompts
│   ├── creative-01-problem-aware.md
│   ├── creative-02-solution-aware.md
│   ├── motion-prompts.md
│   └── kling-video-prompts.md
├── assets/                   ← fotos de referência (testimonials, logo, hero)
├── config/                   ← brand DNA + templates + prompts JSON
│   ├── brand-dna-modifier.md
│   ├── templates.md
│   ├── prompts.json          ← prompts de imagem
│   └── videos.json           ← prompts de vídeo
└── scripts/
    ├── generate_ads.py       ← gera imagens (Nano Banana 2)
    └── generate_videos.py    ← gera vídeos (Veo3 Fast)
```

## Como usar

### Setup
- API key já está em `~/.config/kie/api_key` (chmod 600).
- Dependência: `requests`. Já instalado no sistema.

### Comandos

```bash
cd ads/med-escolha

# Smoke test (1 template, 1 imagem)
python3 scripts/generate_ads.py --smoke

# Batch padrão (todos os 31 templates × 2 imagens cada)
python3 scripts/generate_ads.py

# Mais variações
python3 scripts/generate_ads.py --images 4

# Só alguns templates
python3 scripts/generate_ads.py --templates 1,4,7

# Forçar Nano Banana v1 (mais barato)
python3 scripts/generate_ads.py --v1
```

### Custos (kie.ai credits)

- **Nano Banana 2** (text-to-image): 8 credits por imagem
- **Nano Banana 1 edit** (com `image_urls`): ~5 credits por imagem
- Batch completo (31 × 2): ~480 credits

## Como funciona

1. **Brand Research** (já feito): [brand-dna.md](../../brand-dna.md) na raiz do projeto.
2. **Prompt Generation**: [config/templates.md](config/templates.md) descreve os formatos em forma humana; [config/prompts.json](config/prompts.json) é a versão estruturada que o script lê. O modifier de marca está prepended em cada prompt.
3. **Image Generation**: [scripts/generate_ads.py](scripts/generate_ads.py) faz upload de assets quando `needs_product_images: true`, submete pro endpoint `https://api.kie.ai/api/v1/jobs/createTask`, faz polling, baixa as imagens.

## Modelos kie.ai (verificados 2026-05-11)

| Uso | Model ID |
|-----|----------|
| Text-to-image (v2, padrão) | `nano-banana-2` |
| Text-to-image (v1, fallback) | `google/nano-banana` |
| Image edit com referência | `google/nano-banana-edit` |

## Templates (31 total)

Ver [config/templates.md](config/templates.md) pra detalhes de cada um. Resumo:

- **01-10:** primeira leva (headline, anti-claim, us-vs-them, testimonial Camila, problem-state UGC, stat 570 mil, feature triptych, aspirational hero, press editorial, carousel cover "3 sinais")
- **11-13:** testimonials reais com foto (Vanessa, Júlia, João Vitor)
- **14-16:** stat posters (55 especialidades, 100+ pontos, rotação vs carreira)
- **17-19:** aspirational hero variants (médico jovem, médica sênior, estudante)
- **20-21:** problem-state UGC variants (homem na biblioteca, médica em transição)
- **22-24:** pattern-interrupt (anti-quiz, pricing math, garantia 14 dias)
- **25:** carousel cover "3 verdades incômodas"
- **26-28:** slides 2-4 do carrossel "3 sinais"
- **29-31:** slides 2-4 do carrossel "3 verdades"

## Restrições de copy (importante)

Sempre alinhar com [index.html](../../index.html). Frases proibidas:
- ❌ "orientação de carreira"
- ❌ "sessão com psicóloga"
- ❌ "lives semanais"

Estas não existem no produto. O produto real:
- 100+ perguntas × 55 especialidades = ranking + narrativa
- Acervo de 50+ lives gravadas (não semanais)
- R$ 149, 14 dias de garantia
