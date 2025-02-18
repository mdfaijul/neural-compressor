Step-by-Step
============

This document is used to list steps of reproducing PyTorch se_resnext tuning zoo result.

> **Note**
>
> * PyTorch quantization implementation in imperative path has limitation on automatically execution. It requires to manually add QuantStub and DequantStub for quantizable ops, it also requires to manually do fusion operation.
> * Intel® Neural Compressor supposes user have done these two steps before invoking Intel® Neural Compressor interface.For details, please refer to https://pytorch.org/docs/stable/quantization.html

# Prerequisite

### 1. Installation

#### Python First

Recommend python 3.6 or higher version.

#### Install dependency

```
pip install -r requirements.txt
```

#### Install SE_ResNext model

```Shell
cd examples/pytorch/eager/image_recognition/se_resnext
python setup.py install
```

> **Note**
>
> Please don't install public pretrainedmodels package.

### 2. Prepare Dataset

Download [ImageNet](http://www.image-net.org/) Raw image to dir: /path/to/imagenet. The dir include below folder:

```bash
ls /path/to/imagenet
train  val
```

# Run

### SE_ResNext50_32x4d

```Shell
cd examples/pytorch/eager/image_recognition/se_resnext
python examples/imagenet_eval.py \
          --data /path/to/imagenet \
          -a se_resnext50_32x4d \
          -b 128 \
          -j 1 \
          -t
```

Examples of enabling Intel® Neural Compressor
============================================================

This is a tutorial of how to enable SE_ResNext model with Intel® Neural Compressor.

# User Code Analysis

Intel® Neural Compressor supports three usages:

1. User only provide fp32 "model", and configure calibration dataset, evaluation dataset and metric in model-specific yaml config file.
2. User provide fp32 "model", calibration dataset "q_dataloader" and evaluation dataset "eval_dataloader", and configure metric in tuning.metric field of model-specific yaml config file.
3. User specifies fp32 "model", calibration dataset "q_dataloader" and a custom "eval_func" which encapsulates the evaluation dataset and metric by itself.

As SE_ResNext series are typical classification models, use Top-K as metric which is built-in supported by Intel® Neural Compressor. So here we integrate PyTorch ResNet with Intel® Neural Compressor by the first use case for simplicity.

### Write Yaml Config File

In examples directory, there is conf.yaml. We could remove most of the items and only keep mandatory item for tuning.

```
model:                                               # mandatory. used to specify model specific information.
  name: se_resnext
  framework: pytorch                                 # mandatory. supported values are tensorflow, pytorch, pytorch_ipex, onnxrt_integer, onnxrt_qlinear or mxnet; allow new framework backend extension.

quantization:                                        # optional. tuning constraints on model-wise for advance user to reduce tuning space.
  calibration:
    sampling_size: 256                               # optional. default value is 100. used to set how many samples should be used in calibration.
    dataloader:
      batch_size: 256
      dataset:
        ImageFolder:
          root: /path/to/calibration/dataset         # NOTE: modify to calibration dataset location if needed
      transform:
        RandomResizedCrop:
            size: 224
        RandomHorizontalFlip:
        ToTensor:
        Normalize:
            mean: [0.485, 0.456, 0.406]
            std: [0.229, 0.224, 0.225]

evaluation:                                          # optional. required if user doesn't provide eval_func in neural_compressor.Quantization.
  accuracy:                                          # optional. required if user doesn't provide eval_func in neural_compressor.Quantization.
    metric:
      topk: 1                                        # built-in metrics are topk, map, f1, allow user to register new metric.
    dataloader:
      batch_size: 256
      dataset:
        ImageFolder:
          root: /path/to/evaluation/dataset          # NOTE: modify to evaluation dataset location if needed
      transform:
        Resize:
          size: 256
        CenterCrop:
          size: 224
        ToTensor:
        Normalize:
          mean: [0.485, 0.456, 0.406]
          std: [0.229, 0.224, 0.225]
  performance:                                       # optional. used to benchmark performance of passing model.
    configs:
      cores_per_instance: 4
      num_of_instance: 7
    dataloader:
      batch_size: 1
      dataset:
        ImageFolder:
          root: /path/to/evaluation/dataset          # NOTE: modify to evaluation dataset location if needed
      transform:
        Resize:
          size: 256
        CenterCrop:
          size: 224
        ToTensor:
        Normalize:
          mean: [0.485, 0.456, 0.406]
          std: [0.229, 0.224, 0.225]

tuning:
  accuracy_criterion:
    relative:  0.01                                  # optional. default value is relative, other value is absolute. this example allows relative accuracy loss: 1%.
  exit_policy:
    timeout: 0                                       # optional. tuning timeout (seconds). default value is 0 which means early stop. combine with max_trials field to decide when to exit.
  random_seed: 9527                                  # optional. random seed for deterministic tuning.

```

Here we set accuracy target as tolerating 0.01 relative accuracy loss of baseline. The default tuning strategy is basic strategy. The timeout 0 means unlimited time for a tuning config meet accuracy target.

> **Note** : Neural Compressor does NOT support "mse" tuning strategy for pytorch framework

### Prepare

PyTorch quantization requires two manual steps:

1. Add QuantStub and DeQuantStub for all quantizable ops.
2. Fuse possible patterns, such as Conv + Relu and Conv + BN + Relu. In bert model, there is no fuse pattern.

It's intrinsic limitation of PyTorch quantization imperative path. No way to develop a code to automatically do that.
The related code changes please refer to examples/pytorch/eager/image_recognition/se_resnext/pretrainedmodels/models/senet.py.

### Code Update

After prepare step is done, we just need update imagenet_eval.py like below

```
if args.tune:
        model.eval()
        model.module.fuse_model()
        from neural_compressor.experimental import Quantization, common
        quantizer = Quantization("./conf.yaml")
        quantizer.model = common.Model(model)
        q_model = quantizer()
        return
```

# Original SE_ResNext README

Please refer [SE_ResNext README](SE_ResNext_README.md)
