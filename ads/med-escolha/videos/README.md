# Med Escolha — Videos

**Estrutura:**

```
videos/
├── motion/                  ← 10 motion graphics standalone (Kling v2.1 Pro, 5s, 9:16)
│   ├── motion-01-30-anos-travados/video.mp4
│   ├── motion-02-tres-conselheiros/video.mp4
│   ├── motion-03-55-especialidades-grid/video.mp4
│   ├── motion-04-math-10-vs-55/video.mp4
│   ├── motion-05-pricing-149/video.mp4
│   ├── motion-06-phone-quiz/video.mp4
│   ├── motion-07-3-verdades/video.mp4
│   ├── motion-08-calendar-changing/video.mp4
│   ├── motion-09-ranking-narrativa/video.mp4
│   └── motion-10-fim-do-achismo/video.mp4
├── ugc-creative-01/         ← 4 clips do script de Mariana (Veo3 Fast, 8s, 9:16)
│   ├── ugc-c01-interrupt-creative-01-interrupt-stat/video.mp4
│   ├── ugc-c01-engage-creative-01-engage-doctor-female/video.mp4
│   ├── ugc-c01-educate-creative-01-educate-split-screen/video.mp4
│   └── ugc-c01-offer-creative-01-offer-smartphone-cta/video.mp4
└── ugc-creative-02/         ← 4 clips do script de Pedro (Veo3 Fast, 8s, 9:16)
    ├── ugc-c02-interrupt-creative-02-interrupt-pain-montage/video.mp4
    ├── ugc-c02-engage-creative-02-engage-doctor-male/video.mp4
    ├── ugc-c02-educate-creative-02-educate-math-typography/video.mp4
    └── ugc-c02-offer-creative-02-offer-checkout-cta/video.mp4
```

## Pipelines

### Motion graphics (10 vídeos) — **Nano Banana 2 → Kling v2.1 Pro**

Pipeline image-to-video em 2 passos pra tipografia pixel-perfect:

1. **Nano Banana 2** gera o poster estático 9:16 com tipografia/cores/layout corretos ([singles/32-41-*](../singles))
2. **Kling v2.1 Pro** anima o poster usando prompt de motion direction (5s clip, 9:16)

Cada subpasta contém:
- `video.mp4` — vídeo final
- `motion_prompt.txt` — direção de motion usada no Kling

Custo: ~$0.50 por clip (50 credits) × 10 = ~$5.00.
Tempo: ~6 min por geração.

### UGC ads (8 clips) — **Veo3 Fast** (text-to-video)

Geração direta a partir do prompt. 8s por clip, áudio nativo (a maioria silenciado pra overlay em pós).

Cada subpasta contém:
- `video.mp4` — vídeo final
- `prompt.txt` — prompt completo

Custo: ~$0.20 por clip × 8 = ~$1.60.
Tempo: ~70s por geração.

> Nota: Veo3 Fast tem typography pobre — usado só pros UGC onde a câmera está focada em pessoa, não em texto.

## Como usar

### Motion ads (standalone)
Cada um é um ad completo de 5s. Sobe direto no Meta Ads Manager pra Stories/Reels.

### UGC ads (creative 01 & 02)
Cada criativo tem 4 clips (Interrupt / Engage / Educate / Offer) somando ~32s. Pra montar o ad final ~75s:
1. Junta os 4 clips em ordem no editor
2. Adiciona narração PT-BR (copy em [../copy/creative-01-problem-aware.md](../copy/creative-01-problem-aware.md) e [creative-02](../copy/creative-02-solution-aware.md))
3. Adiciona música de fundo + legendas

## Regenerar / iterar

```bash
cd ads/med-escolha

# Motion ads (Kling pipeline)
python3 scripts/generate_motion_videos_kling.py             # todos os 10
python3 scripts/generate_motion_videos_kling.py --ids 36    # só template 36
python3 scripts/generate_motion_videos_kling.py --smoke     # primeiro só
python3 scripts/generate_motion_videos_kling.py --duration 10  # 10s em vez de 5s

# UGC ads (Veo3 Fast)
python3 scripts/generate_videos.py                          # todos os 18 do videos.json
python3 scripts/generate_videos.py --ids ugc-c01-engage
```
