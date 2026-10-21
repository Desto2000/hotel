"""The Concierge: Hotel's own model stack, beside Mercury.

Mercury writes; the Concierge decides what is relevant, what a source says and
whether a claim holds — locally, in milliseconds, trained on the user's own Reg.

Today (v0):
  embed.py   pretrained local embedder (ONNX via fastembed), behind a small protocol
  index.py   content-addressed vector cache + exact search + reciprocal rank fusion
  labels.py  training/eval data mined from the Reg (annotations, suggestions,
             reviews, verifier verdicts), with retrieval-quality stats, and
             invalidation of exported data when content is forgotten
  pointer.py zero-shot quote pointer (closest source sentence to a claim)

Later: the fine-tuned heads (embedding, pointer, verifier, structure) plug in
behind the same interfaces; each checkpoint records the Reg head it was trained
on, so replay can evaluate it and `forget` can invalidate it.
"""
