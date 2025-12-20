## Detecting Backdoor Attacks in Federated Learning via Direction Alignment Inspection

This is the official implementation for CVPR'25 **✨Highlight✨** paper "Detecting Backdoor Attacks in Federated Learning via Direction Alignment Inspection".

You can find the full version of our paper [here][paper].

[paper]: https://arxiv.org/abs/2503.07978

## Related Methods

If you found AlignIns is interesting, you may also refer to our other related works on robust federated learning systems:

- **OBA & BNGuard** (MobiHoc 2025, **Oral presentation**): https://github.com/JiiahaoXU/SoDa-BNGuard  
- **MASA** (WACV 2025): https://github.com/JiiahaoXU/MASA  
- **LASA** (WACV 2025, **Oral presentation**): https://github.com/JiiahaoXU/LASA

All these methods are based on simple and effective ideas for protecting federated learning systems. They are easy to implement and can be directly integrated into this codebase or into existing federated learning frameworks.

## Usage

If you have any issues using this repo, feel free to contact Jiahao @ jiahaox@unr.edu.

The proposed aggregation rule AlignIns is placed in `src/aggregation.py`, and you can easily take it and integrate AlignIns with your code.

### Environment

Our code does not rely on special libraries or tools, so it can be easily integrated with most environment settings. 

If you want to use the same settings as us, we provide the conda environment we used in `env.yaml` for your convenience.

### Dataset

CIFAR-10 and CIFAR-100 datasets are available on `torchvision` and will be downloaded automatically.

Tiny-ImageNet can be easily downloaded from Kaggle.

### Example

Generally, to run a case with default settings, you can easily use the following command:

```
python federated.py \
--poison_frac 0.3 --num_corrupt 4 \
--aggr alignins --data cifar10 --attack badnet
```

If you want to run a case with non-IID settings, you can easily use the following command:

```
python federated.py \
--poison_frac 0.3 --num_corrupt 4 \
--non_iid --beta 0.5 \
--aggr alignins --data cifar10 --attack badnet
```

Here,

| Argument        | Type       | Description   | Choice |
|-----------------|------------|---------------|--------|
| `aggr`         | str   | Defense method applied by the server | avg, alignins, rlr, mkrum, mmetric, lockdown, foolsgold, rfa|
| `data`    |   str     | Main task data        | cifar10, cifar100, tinyimagenet |
| `num_agents`         | int | Number of clients in FL   | N/A |
| `attack`         | str | Attack method   | badnet, DBA, neurotoxin, pgd |
| `poison_frac`         | float | Data poisoning ratio   | [0.0, 1.0] |
| `num_corrupt`         | int | Number of malicious clients in FL   | [0, num_agents//2-1] |
| `non_iid`         | store_true | Enable non-IID settings or not      | N/A |
| `beta`         | float | Data heterogeneous degree     | [0.1, 1.0]|

For other arguments, you can check the `federated.py` file where the detailed explanation is presented.

## Citation

We provide the following BibTeX entry for citation for your convenience.

```
@InProceedings{Xu_2025_CVPR_AlignIns,
    author    = {Xu, Jiahao and Zhang, Zikai and Hu, Rui},
    title     = {Detecting Backdoor Attacks in Federated Learning via Direction Alignment Inspection},
    booktitle = {Proceedings of the Computer Vision and Pattern Recognition Conference (CVPR)},
    month     = {June},
    year      = {2025},
    pages     = {20654-20664}
}

```

## Acknowledgment
Our code is partially constructed on https://github.com/git-disl/Lockdown, big thanks to their contribution!
