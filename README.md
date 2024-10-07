<h1 align="center">SPARO: Selective Attention for Robust and Compositional Transformer Encodings for Vision</h1>
<h3 align="center">Ankit Vani, Bac Nguyen, Samuel Lavoie, Ranjay Krishna, Aaron Courville</h3>
<h3 align="center">Published at ECCV 2024</h3>

### [[Paper, arXiv]](https://arxiv.org/abs/2404.15721)

**Abstract**: Selective attention helps us focus on task-relevant aspects in the constant flood of our sensory input. This constraint in our perception allows us to robustly generalize under distractions and to new compositions of perceivable concepts. Transformers employ a similar notion of attention in their architecture, but representation learning models with transformer backbones like CLIP and DINO often fail to demonstrate robustness and compositionality. We highlight a missing architectural prior: unlike human perception, transformer encodings do not separately attend over individual concepts. In response, we propose SPARO, a read-out mechanism that partitions encodings into separately-attended slots, each produced by a single attention head. Using SPARO with CLIP imparts an inductive bias that the vision and text modalities are different views of a shared compositional world with the same corresponding concepts. Using SPARO, we demonstrate improvements on downstream recognition, robustness, retrieval, and compositionality benchmarks with CLIP (up to +14% for ImageNet, +4% for SugarCrepe), and on nearest neighbors and linear probe for ImageNet with DINO (+3% each). We also showcase a powerful ability to intervene and select individual SPARO concepts to further improve downstream task performance (up from +4% to +9% for SugarCrepe) and use this ability to study the robustness of SPARO's representation structure. Finally, we provide insights through ablation experiments and visualization of learned concepts.

##

### This repository demonstrates the incorporation of SPARO onto the CLIP encoders.

Demo scripts for training models on a single GPU are provided in `scripts/`.

This code is based on [OpenCLIP v2.20.0](https://github.com/mlfoundations/open_clip/tree/v2.20.0).

##

### Citation
```
@inproceedings{vani2024sparo,
  title={{SPARO}: Selective Attention for Robust and Compositional Transformer Encodings for Vision},
  author={Ankit Vani and Bac Nguyen and Samuel Lavoie and Ranjay Krishna and Aaron Courville},
  booktitle = {European Conference on Computer Vision},
  year={2024},
}
```
