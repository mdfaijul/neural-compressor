# coding=utf-8
# Copyright 2018 The Google AI Language Team Authors and The HuggingFace Inc. team.
# Copyright (c) 2018, NVIDIA CORPORATION.  All rights reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
""" Finetuning the library models for sequence classification on GLUE (Bert, XLM, XLNet, RoBERTa)."""

from __future__ import absolute_import, division, print_function

import argparse
import glob
import logging
import os
import random
import json

import numpy as np
import torch
from torch.utils.data import (DataLoader, RandomSampler, SequentialSampler,
                              TensorDataset)
from torch.utils.data.distributed import DistributedSampler
from torch.quantization import quantize, prepare, convert, propagate_qconfig_, add_observer_
from torch.quantization import \
    QuantWrapper, QuantStub, DeQuantStub, default_qconfig, default_per_channel_qconfig

try:
    from torch.utils.tensorboard import SummaryWriter
except:
    from tensorboardX import SummaryWriter

from tqdm import tqdm, trange

from transformers import (WEIGHTS_NAME, BertConfig,
                          BertForSequenceClassification, BertTokenizer,
                          RobertaConfig,
                          RobertaForSequenceClassification,
                          RobertaTokenizer,
                          XLMConfig, XLMForSequenceClassification,
                          XLMTokenizer, XLNetConfig,
                          XLNetForSequenceClassification,
                          XLNetTokenizer,
                          DistilBertConfig,
                          DistilBertForSequenceClassification,
                          DistilBertTokenizer,
                          AlbertConfig,
                          AlbertForSequenceClassification, 
                          AlbertTokenizer,
                          CamembertConfig, 
                          CamembertForSequenceClassification, 
                          CamembertTokenizer)

from transformers import AdamW, get_linear_schedule_with_warmup

from transformers import glue_compute_metrics as compute_metrics
from transformers import glue_output_modes as output_modes
from transformers import glue_processors as processors
from transformers import glue_convert_examples_to_features as convert_examples_to_features

logger = logging.getLogger(__name__)

ALL_MODELS = sum((tuple(conf.pretrained_config_archive_map.keys()) for conf in (BertConfig, XLNetConfig, XLMConfig, 
                                                                                RobertaConfig, DistilBertConfig)), ())

MODEL_CLASSES = {
    'bert': (BertConfig, BertForSequenceClassification, BertTokenizer),
    'xlnet': (XLNetConfig, XLNetForSequenceClassification, XLNetTokenizer),
    'xlm': (XLMConfig, XLMForSequenceClassification, XLMTokenizer),
    'roberta': (RobertaConfig, RobertaForSequenceClassification, RobertaTokenizer),
    'distilbert': (DistilBertConfig, DistilBertForSequenceClassification, DistilBertTokenizer),
    'albert': (AlbertConfig, AlbertForSequenceClassification, AlbertTokenizer),
    'camembert': (CamembertConfig, CamembertForSequenceClassification, CamembertTokenizer)
}


def set_seed(args):
    random.seed(args.seed)
    np.random.seed(args.seed)
    torch.manual_seed(args.seed)
    if args.n_gpu > 0:
        torch.cuda.manual_seed_all(args.seed)


def train(args, train_dataset, model, tokenizer):
    """ Train the model """
    if args.local_rank in [-1, 0]:
        tb_writer = SummaryWriter()

    args.train_batch_size = args.per_gpu_train_batch_size * max(1, args.n_gpu)
    train_sampler = RandomSampler(train_dataset) if args.local_rank == -1 else DistributedSampler(train_dataset)
    train_dataloader = DataLoader(train_dataset, sampler=train_sampler, batch_size=args.train_batch_size)

    if args.max_steps > 0:
        t_total = args.max_steps
        args.num_train_epochs = args.max_steps // (len(train_dataloader) // args.gradient_accumulation_steps) + 1
    else:
        t_total = len(train_dataloader) // args.gradient_accumulation_steps * args.num_train_epochs

    # Prepare optimizer and schedule (linear warmup and decay)
    no_decay = ['bias', 'LayerNorm.weight']
    optimizer_grouped_parameters = [
        {'params': [p for n, p in model.named_parameters() if not any(nd in n for nd in no_decay)], 'weight_decay': args.weight_decay},
        {'params': [p for n, p in model.named_parameters() if any(nd in n for nd in no_decay)], 'weight_decay': 0.0}
        ]

    optimizer = AdamW(optimizer_grouped_parameters, lr=args.learning_rate, eps=args.adam_epsilon)
    scheduler = get_linear_schedule_with_warmup(optimizer, num_warmup_steps=args.warmup_steps, num_training_steps=t_total)
    if args.fp16:
        try:
            from apex import amp
        except ImportError:
            raise ImportError("Please install apex from https://www.github.com/nvidia/apex to use fp16 training.")
        model, optimizer = amp.initialize(model, optimizer, opt_level=args.fp16_opt_level)

    # multi-gpu training (should be after apex fp16 initialization)
    if args.n_gpu > 1:
        model = torch.nn.DataParallel(model)

    # Distributed training (should be after apex fp16 initialization)
    if args.local_rank != -1:
        model = torch.nn.parallel.DistributedDataParallel(model, device_ids=[args.local_rank],
                                                          output_device=args.local_rank,
                                                          find_unused_parameters=True)

    # Train!
    logger.info("***** Running training *****")
    logger.info("  Num examples = %d", len(train_dataset))
    logger.info("  Num Epochs = %d", args.num_train_epochs)
    logger.info("  Instantaneous batch size per GPU = %d", args.per_gpu_train_batch_size)
    logger.info("  Total train batch size (w. parallel, distributed & accumulation) = %d",
                   args.train_batch_size * args.gradient_accumulation_steps * (torch.distributed.get_world_size() if args.local_rank != -1 else 1))
    logger.info("  Gradient Accumulation steps = %d", args.gradient_accumulation_steps)
    logger.info("  Total optimization steps = %d", t_total)

    global_step = 0
    tr_loss, logging_loss = 0.0, 0.0
    model.zero_grad()
    train_iterator = trange(int(args.num_train_epochs), desc="Epoch", disable=args.local_rank not in [-1, 0])
    set_seed(args)  # Added here for reproductibility (even between python 2 and 3)
    for _ in train_iterator:
        epoch_iterator = tqdm(train_dataloader, desc="Iteration", disable=args.local_rank not in [-1, 0])
        for step, batch in enumerate(epoch_iterator):
            model.train()
            batch = tuple(t.to(args.device) for t in batch)
            inputs = {'input_ids':      batch[0],
                      'attention_mask': batch[1],
                      'labels':         batch[3]}
            if args.model_type != 'distilbert':
                inputs['token_type_ids'] = batch[2] if args.model_type in ['bert', 'xlnet'] else None  # XLM, DistilBERT and RoBERTa don't use segment_ids
            outputs = model(**inputs)
            loss = outputs[0]  # model outputs are always tuple in transformers (see doc)

            if args.n_gpu > 1:
                loss = loss.mean() # mean() to average on multi-gpu parallel training
            if args.gradient_accumulation_steps > 1:
                loss = loss / args.gradient_accumulation_steps

            if args.fp16:
                with amp.scale_loss(loss, optimizer) as scaled_loss:
                    scaled_loss.backward()
            else:
                loss.backward()

            tr_loss += loss.item()
            if (step + 1) % args.gradient_accumulation_steps == 0:
                if args.fp16:
                    torch.nn.utils.clip_grad_norm_(amp.master_params(optimizer), args.max_grad_norm)
                else:
                    torch.nn.utils.clip_grad_norm_(model.parameters(), args.max_grad_norm)

                optimizer.step()
                scheduler.step()  # Update learning rate schedule
                model.zero_grad()
                global_step += 1

                if args.local_rank in [-1, 0] and args.logging_steps > 0 and global_step % args.logging_steps == 0:
                    logs = {}
                    if args.local_rank == -1 and args.evaluate_during_training:  # Only evaluate when single GPU otherwise metrics may not average well
                        results = evaluate(args, model, tokenizer)
                        for key, value in results.items():
                            eval_key = 'eval_{}'.format(key)
                            logs[eval_key] = value

                    loss_scalar = (tr_loss - logging_loss) / args.logging_steps
                    learning_rate_scalar = scheduler.get_lr()[0]
                    logs['learning_rate'] = learning_rate_scalar
                    logs['loss'] = loss_scalar
                    logging_loss = tr_loss

                    for key, value in logs.items():
                        tb_writer.add_scalar(key, value, global_step)
                    print(json.dumps({**logs, **{'step': global_step}}))

                if args.local_rank in [-1, 0] and args.save_steps > 0 and global_step % args.save_steps == 0:
                    # Save model checkpoint
                    output_dir = os.path.join(args.output_dir, 'checkpoint-{}'.format(global_step))
                    if not os.path.exists(output_dir):
                        os.makedirs(output_dir)
                    model_to_save = model.module if hasattr(model, 'module') else model  # Take care of distributed/parallel training
                    model_to_save.save_pretrained(output_dir)
                    torch.save(args, os.path.join(output_dir, 'training_args.bin'))
                    logger.info("Saving model checkpoint to %s", output_dir)

            if args.max_steps > 0 and global_step > args.max_steps:
                epoch_iterator.close()
                break
        if args.max_steps > 0 and global_step > args.max_steps:
            train_iterator.close()
            break

    if args.local_rank in [-1, 0]:
        tb_writer.close()

    return global_step, tr_loss / global_step

def fallback_layer(model, layer_name="", exculde_layers={}):
    r"""
    force layers in exculde_layers bucket to be fp32 op, the model
    should be add qconfig and QuantWrapper already
    Args:
        model: a fp32 model with qconfig information
        layer_name: model layer name format is : xxx.xxx.xxx
        : flag to save, only save the first one batch tensor
    """

    for name, sub_model in list(model.named_children()):
        sub_model_layer_name = layer_name + name + "."
        if sub_model_layer_name in exculde_layers:
           print("fallback_layer:", sub_model_layer_name)
           sub_model.qconfig = None
           for name_tmp, sub_model_tmp in list(model.named_children()):
                if (isinstance(sub_model_tmp, QuantStub) or isinstance(sub_model_tmp, DeQuantStub)):
                       model._modules[name_tmp]=torch.nn.Identity()
        else:
           fallback_layer(sub_model, sub_model_layer_name, exculde_layers)


def evaluate(args, model, tokenizer, prefix="", calibration=False):
    # Loop to handle MNLI double evaluation (matched, mis-matched)
    eval_task_names = ("mnli", "mnli-mm") if args.task_name == "mnli" else (args.task_name,)

    results = {}
    for eval_task in eval_task_names:
        eval_dataset = load_and_cache_examples(args, eval_task, tokenizer, evaluate=True)

        if calibration:
           args.eval_batch_size = 16
        else:
           args.eval_batch_size = args.per_gpu_eval_batch_size * max(1, args.n_gpu)
        args.eval_batch_size = args.per_gpu_eval_batch_size * max(1, args.n_gpu)
        # Note that DistributedSampler samples randomly
        eval_sampler = SequentialSampler(eval_dataset)
        eval_dataloader = DataLoader(eval_dataset, sampler=eval_sampler, batch_size=args.eval_batch_size)
        calibation_iteration = int((len(eval_dataset) * 0.05 + args.eval_batch_size - 1) / args.eval_batch_size)
        # multi-gpu eval
        if args.n_gpu > 1:
            model = torch.nn.DataParallel(model)

        # Eval!
        logger.info("***** Running evaluation {} *****".format(prefix))
        logger.info("  Num examples = %d", len(eval_dataset))
        print("  Batch size = %d" % args.eval_batch_size)
        eval_loss = 0.0
        nb_eval_steps = 0
        preds = None
        out_label_ids = None
        if args.mkldnn_eval:
            from torch.utils import mkldnn as mkldnn_utils
            model = mkldnn_utils.to_mkldnn(model)
            print(model)

        import timeit
        total_time = 0.0
        for batch in tqdm(eval_dataloader, desc="Evaluating"):
            model.eval()
            batch = tuple(t.to(args.device) for t in batch)
            if calibration and nb_eval_steps >= calibation_iteration:
                break
            with torch.no_grad():
                inputs = {'input_ids':      batch[0],
                          'attention_mask': batch[1],
                          'labels':         batch[3]}
                if args.model_type != 'distilbert':
                    inputs['token_type_ids'] = batch[2] if args.model_type in ['bert', 'xlnet'] else None  # XLM, DistilBERT and RoBERTa don't use segment_ids
                if nb_eval_steps >= args.warmup:
                    start = timeit.default_timer()
                outputs = model(**inputs)
                if nb_eval_steps >= args.warmup:
                    total_time += (timeit.default_timer() - start)
                tmp_eval_loss, logits = outputs[:2]

                eval_loss += tmp_eval_loss.mean().item()
            nb_eval_steps += 1
            if args.do_bf16:
                if preds is None:
                    preds = logits.detach().cpu().to(torch.float).numpy()
                    out_label_ids = inputs['labels'].detach().cpu().to(torch.float).numpy()
                else:
                    preds = np.append(preds, logits.detach().cpu().to(torch.float).numpy(), axis=0)
                    out_label_ids = np.append(out_label_ids, inputs['labels'].detach().cpu().to(torch.float).numpy(), axis=0)
            else:
                if preds is None:
                    preds = logits.detach().cpu().numpy()
                    out_label_ids = inputs['labels'].detach().cpu().numpy()
                else:
                    preds = np.append(preds, logits.detach().cpu().numpy(), axis=0)
                    out_label_ids = np.append(out_label_ids, inputs['labels'].detach().cpu().numpy(), axis=0)

            if args.iter > 0 and nb_eval_steps > (args.warmup + args.iter):
                break
        if nb_eval_steps >= args.warmup:
            perf = (nb_eval_steps - args.warmup) * args.eval_batch_size / total_time
            if args.eval_batch_size == 1:
                print('Latency: %.3f ms' % (total_time / (nb_eval_steps - args.warmup) * 1000))
            print("Throughput: {} samples/s".format(perf))
        else:
            logger.info("*****no performance, please check dataset length and warmup number *****")
        eval_loss = eval_loss / nb_eval_steps
        if args.output_mode == "classification":
            preds = np.argmax(preds, axis=1)
        elif args.output_mode == "regression":
            preds = np.squeeze(preds)
        result = compute_metrics(eval_task, preds, out_label_ids)
        results.update(result)

        bert_task_acc_keys = ['acc_and_f1', 'f1', 'mcc', 'spearmanr', 'acc']
        for key in bert_task_acc_keys:
            if key in result.keys():
                acc = result[key]
                break
        print("Accuracy: %.5f" % acc)
        logger.info("***** Eval results {} *****".format(prefix))
        for key in sorted(result.keys()):
            logger.info("  %s = %s", key, str(result[key]))

    return results, perf

def load_and_cache_examples(args, task, tokenizer, evaluate=False):
    if args.local_rank not in [-1, 0] and not evaluate:
        torch.distributed.barrier()  # Make sure only the first process in distributed training process the dataset, and the others will use the cache

    processor = processors[task]()
    output_mode = output_modes[task]
    # Load data features from cache or dataset file
    if not os.path.exists("./dataset_cached"):
        os.makedirs("./dataset_cached")
    cached_features_file = os.path.join("./dataset_cached", 'cached_{}_{}_{}_{}'.format(
        'dev' if evaluate else 'train',
        list(filter(None, args.model_name_or_path.split('/'))).pop(),
        str(args.max_seq_length),
        str(task)))
    if os.path.exists(cached_features_file) and not args.overwrite_cache:
        logger.info("Loading features from cached file %s", cached_features_file)
        features = torch.load(cached_features_file)
    else:
        logger.info("Creating features from dataset file at %s", args.data_dir)
        label_list = processor.get_labels()
        if task in ['mnli', 'mnli-mm'] and args.model_type in ['roberta']:
            # HACK(label indices are swapped in RoBERTa pretrained model)
            label_list[1], label_list[2] = label_list[2], label_list[1] 
        examples = processor.get_dev_examples(args.data_dir) if evaluate else processor.get_train_examples(args.data_dir)
        features = convert_examples_to_features(examples,
                                                tokenizer,
                                                label_list=label_list,
                                                max_length=args.max_seq_length,
                                                output_mode=output_mode,
                                                pad_on_left=bool(args.model_type in ['xlnet']),                 # pad on the left for xlnet
                                                pad_token=tokenizer.convert_tokens_to_ids([tokenizer.pad_token])[0],
                                                pad_token_segment_id=4 if args.model_type in ['xlnet'] else 0,
        )
        if args.local_rank in [-1, 0]:
            logger.info("Saving features into cached file %s", cached_features_file)
            torch.save(features, cached_features_file)

    if args.local_rank == 0 and not evaluate:
        torch.distributed.barrier()  # Make sure only the first process in distributed training process the dataset, and the others will use the cache

    # Convert to Tensors and build dataset
    all_input_ids = torch.tensor([f.input_ids for f in features], dtype=torch.long)
    all_attention_mask = torch.tensor([f.attention_mask for f in features], dtype=torch.long)
    all_token_type_ids = torch.tensor([f.token_type_ids for f in features], dtype=torch.long)
    if output_mode == "classification":
        all_labels = torch.tensor([f.label for f in features], dtype=torch.long)
    elif output_mode == "regression":
        all_labels = torch.tensor([f.label for f in features], dtype=torch.float)

    dataset = TensorDataset(all_input_ids, all_attention_mask, all_token_type_ids, all_labels)
    return dataset

class Bert_DataLoader(DataLoader):
    def __init__(self, loader=None, model_type=None, device='cpu'):
        self.loader = loader
        self.model_type = model_type
        self.device = device
    def __iter__(self):
        for batch in self.loader:
            batch = tuple(t.to(self.device) for t in batch)
            outputs = {'input_ids':      batch[0],
                      'attention_mask': batch[1],
                      'labels':         batch[3]}
            if self.model_type != 'distilbert':
                outputs['token_type_ids'] = batch[2] if self.model_type in ['bert', 'xlnet'] else None  # XLM, DistilBERT and RoBERTa don't use segment_ids
            yield outputs, outputs['labels']

def main():
    parser = argparse.ArgumentParser()

    ## Required parameters
    parser.add_argument("--data_dir", default=None, type=str, required=True,
                        help="The input data dir. Should contain the .tsv files (or other data files) for the task.")
    parser.add_argument("--model_type", default=None, type=str, required=True,
                        help="Model type selected in the list: " + ", ".join(MODEL_CLASSES.keys()))
    parser.add_argument("--model_name_or_path", default=None, type=str, required=True,
                        help="Path to pre-trained model or shortcut name selected in the list;" + ", ".join(ALL_MODELS))
    parser.add_argument("--task_name", default=None, type=str, required=True,
                        help="The name of the task to train selected in the list: " + ", ".join(processors.keys()))
    parser.add_argument("--output_dir", default=None, type=str, required=True,
                        help="The output directory where the model predictions and checkpoints will be written.")

    ## Other parameters
    parser.add_argument("--config_name", default="", type=str,
                        help="Pretrained config name or path if not the same as model_name")
    parser.add_argument("--tokenizer_name", default="", type=str,
                        help="Pretrained tokenizer name or path if not the same as model_name")
    parser.add_argument("--cache_dir", default="", type=str,
                        help="Where do you want to store the pre-trained models downloaded from s3")
    parser.add_argument("--max_seq_length", default=128, type=int,
                        help="The maximum total input sequence length after tokenization. Sequences longer "
                             "than this will be truncated, sequences shorter will be padded.")
    parser.add_argument("--do_train", action='store_true',
                        help="Whether to run training.")
    parser.add_argument("--do_eval", action='store_true',
                        help="Whether to run eval on the dev set.")
    parser.add_argument("--evaluate_during_training", action='store_true',
                        help="Rul evaluation during training at each logging step.")
    parser.add_argument("--do_lower_case", action='store_true',
                        help="Set this flag if you are using an uncased model.")

    parser.add_argument("--per_gpu_train_batch_size", default=8, type=int,
                        help="Batch size per GPU/CPU for training.")
    parser.add_argument("--per_gpu_eval_batch_size", default=8, type=int,
                        help="Batch size per GPU/CPU for evaluation.")
    parser.add_argument('--gradient_accumulation_steps', type=int, default=1,
                        help="Number of updates steps to accumulate before performing a backward/update pass.")     
    parser.add_argument("--learning_rate", default=5e-5, type=float,
                        help="The initial learning rate for Adam.")
    parser.add_argument("--weight_decay", default=0.0, type=float,
                        help="Weight deay if we apply some.")
    parser.add_argument("--adam_epsilon", default=1e-8, type=float,
                        help="Epsilon for Adam optimizer.")
    parser.add_argument("--max_grad_norm", default=1.0, type=float,
                        help="Max gradient norm.")
    parser.add_argument("--num_train_epochs", default=3.0, type=float,
                        help="Total number of training epochs to perform.")
    parser.add_argument("--max_steps", default=-1, type=int,
                        help="If > 0: set total number of training steps to perform. Override num_train_epochs.")
    parser.add_argument("--warmup_steps", default=0, type=int,
                        help="Linear warmup over warmup_steps.")

    parser.add_argument('--logging_steps', type=int, default=50,
                        help="Log every X updates steps.")
    parser.add_argument('--save_steps', type=int, default=50,
                        help="Save checkpoint every X updates steps.")
    parser.add_argument("--eval_all_checkpoints", action='store_true',
                        help="Evaluate all checkpoints starting with the same prefix as model_name ending and ending with step number")
    parser.add_argument("--no_cuda", action='store_true',
                        help="Avoid using CUDA when available")
    parser.add_argument("--mkldnn_eval", action='store_true',
                        help="evaluation with MKLDNN")
    parser.add_argument("--mkldnn_train", action='store_true',
                        help="training with MKLDNN")
    parser.add_argument('--overwrite_output_dir', action='store_true',
                        help="Overwrite the content of the output directory")
    parser.add_argument('--overwrite_cache', action='store_true',
                        help="Overwrite the cached training and evaluation sets")
    parser.add_argument('--seed', type=int, default=42,
                        help="random seed for initialization")

    parser.add_argument('--fp16', action='store_true',
                        help="Whether to use 16-bit (mixed) precision (through NVIDIA apex) instead of 32-bit")
    parser.add_argument('--fp16_opt_level', type=str, default='O1',
                        help="For fp16: Apex AMP optimization level selected in ['O0', 'O1', 'O2', and 'O3']."
                             "See details at https://nvidia.github.io/apex/amp.html")
    parser.add_argument("--local_rank", type=int, default=-1,
                        help="For distributed training: local_rank")
    parser.add_argument('--server_ip', type=str, default='', help="For distant debugging.")
    parser.add_argument('--server_port', type=str, default='', help="For distant debugging.")
    parser.add_argument("--do_fp32_inference", action='store_true',
                        help="Whether to run fp32 inference.")
    parser.add_argument("--do_calibration", action='store_true',
                        help="Whether to do calibration.")
    parser.add_argument("--do_int8_inference", action='store_true',
                        help="Whether to run int8 inference.")
    parser.add_argument("--do_bf16", action='store_true',
                        help="run bf16 evaluation / training.")
    parser.add_argument("--tune", action='store_true',
                        help="run Neural Compressor to tune int8 acc.")
    parser.add_argument("--warmup", type=int, default=2,
                        help="warmup for performance")
    parser.add_argument('-i', "--iter", default=0, type=int,
                        help='For accuracy measurement only.')
    parser.add_argument('--config', default='conf.yaml', type=str,
                        help='yaml config file path') 
    parser.add_argument('--benchmark', dest='benchmark', action='store_true',
                        help='run benchmark')
    parser.add_argument('-r', "--accuracy_only", dest='accuracy_only', action='store_true',
                        help='For accuracy measurement only.')
    parser.add_argument("--tuned_checkpoint", default='./saved_results', type=str, metavar='PATH',
                        help='path to checkpoint tuned by Neural Compressor (default: ./)')
    parser.add_argument('--int8', dest='int8', action='store_true',
                        help='run benchmark')

    args = parser.parse_args()

    if os.path.exists(args.output_dir) and os.listdir(args.output_dir) and args.do_train and not args.overwrite_output_dir:
        raise ValueError("Output directory ({}) already exists and is not empty. Use --overwrite_output_dir to overcome.".format(args.output_dir))

    # Setup distant debugging if needed
    if args.server_ip and args.server_port:
        # Distant debugging - see https://code.visualstudio.com/docs/python/debugging#_attach-to-a-local-script
        import ptvsd
        print("Waiting for debugger attach")
        ptvsd.enable_attach(address=(args.server_ip, args.server_port), redirect_output=True)
        ptvsd.wait_for_attach()

    # Setup CUDA, GPU & distributed training
    if args.local_rank == -1 or args.no_cuda:
        device = torch.device("cuda" if torch.cuda.is_available() and not args.no_cuda else "cpu")
        args.n_gpu = torch.cuda.device_count()
    else:  # Initializes the distributed backend which will take care of sychronizing nodes/GPUs
        torch.cuda.set_device(args.local_rank)
        device = torch.device("cuda", args.local_rank)
        torch.distributed.init_process_group(backend='nccl')
        args.n_gpu = 1
    args.device = device

    # Setup logging
    logging.basicConfig(format = '%(asctime)s - %(levelname)s - %(name)s -   %(message)s',
                        datefmt = '%m/%d/%Y %H:%M:%S',
                        level = logging.INFO if args.local_rank in [-1, 0] else logging.WARN)
    logger.warning("Process rank: %s, device: %s, n_gpu: %s, distributed training: %s, 16-bits training: %s",
                    args.local_rank, device, args.n_gpu, bool(args.local_rank != -1), args.fp16)

    # Set seed
    set_seed(args)

    # Prepare GLUE task
    args.task_name = args.task_name.lower()
    if args.task_name not in processors:
        raise ValueError("Task not found: %s" % (args.task_name))
    processor = processors[args.task_name]()
    args.output_mode = output_modes[args.task_name]
    label_list = processor.get_labels()
    num_labels = len(label_list)
    mix_qkv = False
    if args.do_calibration or args.do_int8_inference or args.tune:
       mix_qkv = True 
    # Load pretrained model and tokenizer
    if args.local_rank not in [-1, 0]:
        torch.distributed.barrier()  # Make sure only the first process in distributed training will download model & vocab

    args.model_type = args.model_type.lower()
    config_class, model_class, tokenizer_class = MODEL_CLASSES[args.model_type]
    config = config_class.from_pretrained(args.config_name if args.config_name else args.model_name_or_path,
                                          num_labels=num_labels,
                                          finetuning_task=args.task_name,
                                          cache_dir=args.cache_dir if args.cache_dir else None)
    tokenizer = tokenizer_class.from_pretrained(args.tokenizer_name if args.tokenizer_name else args.model_name_or_path,
                                                do_lower_case=args.do_lower_case,
                                                cache_dir=args.cache_dir if args.cache_dir else None)
    model = model_class.from_pretrained(args.model_name_or_path,
                                        from_tf=bool('.ckpt' in args.model_name_or_path),
                                        config=config,
                                        mix_qkv=mix_qkv,
                                        bf16=args.do_bf16,
                                        mkldnn_train=args.mkldnn_train,
                                        cache_dir=args.cache_dir if args.cache_dir else None)

    if args.local_rank == 0:
        torch.distributed.barrier()  # Make sure only the first process in distributed training will download model & vocab

    model.to(args.device)

    logger.info("Training/evaluation parameters %s", args)


    # Training
    if args.do_train:
        train_dataset = load_and_cache_examples(args, args.task_name, tokenizer, evaluate=False)
        global_step, tr_loss = train(args, train_dataset, model, tokenizer)
        logger.info(" global_step = %s, average loss = %s", global_step, tr_loss)


    # Saving best-practices: if you use defaults names for the model, you can reload it using from_pretrained()
    if args.do_train and (args.local_rank == -1 or torch.distributed.get_rank() == 0):
        # Create output directory if needed
        if not os.path.exists(args.output_dir) and args.local_rank in [-1, 0]:
            os.makedirs(args.output_dir)

        logger.info("Saving model checkpoint to %s", args.output_dir)
        # Save a trained model, configuration and tokenizer using `save_pretrained()`.
        # They can then be reloaded using `from_pretrained()`
        model_to_save = model.module if hasattr(model, 'module') else model  # Take care of distributed/parallel training
        model_to_save.save_pretrained(args.output_dir)
        tokenizer.save_pretrained(args.output_dir)

        # Good practice: save your training arguments together with the trained model
        torch.save(args, os.path.join(args.output_dir, 'training_args.bin'))

        # Load a trained model and vocabulary that you have fine-tuned
        model = model_class.from_pretrained(args.output_dir)
        tokenizer = tokenizer_class.from_pretrained(args.output_dir)
        model.to(args.device)


    # Evaluation
    results = {}
    if args.do_eval and args.local_rank in [-1, 0]:
        tokenizer = tokenizer_class.from_pretrained(args.output_dir, do_lower_case=args.do_lower_case)
        checkpoints = [args.output_dir]
        if args.eval_all_checkpoints:
            checkpoints = list(os.path.dirname(c) for c in sorted(glob.glob(args.output_dir + '/**/' + WEIGHTS_NAME, recursive=True)))
            logging.getLogger("transformers.modeling_utils").setLevel(logging.WARN)  # Reduce logging
        logger.info("Evaluate the following checkpoints: %s", checkpoints)
        for checkpoint in checkpoints:
            global_step = checkpoint.split('-')[-1] if len(checkpoints) > 1 else ""
            prefix = checkpoint.split('/')[-1] if checkpoint.find('checkpoint') != -1 else ""
            
            logger.info("Evaluate:" + args.task_name)
            if args.mkldnn_eval or args.do_fp32_inference or args.do_bf16:
               model = model_class.from_pretrained(checkpoint)
               model.to(args.device)
               result = evaluate(args, model, tokenizer, prefix=prefix)
               result = dict((k + '_{}'.format(global_step), v) for k, v in result.items())
               results.update(result)

            if args.tune:
                def eval_func_for_nc(model):
                    result, perf = evaluate(args, model, tokenizer, prefix=prefix)
                    bert_task_acc_keys = ['acc_and_f1', 'f1', 'mcc', 'spearmanr', 'acc']
                    for key in bert_task_acc_keys:
                        if key in result.keys():
                            logger.info("Finally Eval {}:{}".format(key, result[key]))
                            acc = result[key]
                            break
                    return acc

                model = model_class.from_pretrained(checkpoint, mix_qkv=True)
                model.to(args.device)
                eval_task_names = ("mnli", "mnli-mm") if args.task_name == "mnli" else (args.task_name,)

                for eval_task in eval_task_names:
                    eval_dataset = load_and_cache_examples(args, eval_task, tokenizer, evaluate=True)

                    args.eval_batch_size = args.per_gpu_eval_batch_size * max(1, args.n_gpu)
                    # multi-gpu eval
                    if args.n_gpu > 1:
                        model = torch.nn.DataParallel(model)

                    if args.mkldnn_eval:
                        from torch.utils import mkldnn as mkldnn_utils
                        model = mkldnn_utils.to_mkldnn(model)
                        print(model)
                    from neural_compressor.experimental import Quantization, common
                    quantizer = Quantization(args.config)
                    if eval_task != "squad":
                        eval_task = 'classifier'
                    eval_dataset = quantizer.dataset('bert', dataset=eval_dataset,
                                                     task=eval_task, model_type=args.model_type)
                    quantizer.model = common.Model(model)
                    quantizer.calib_dataloader = common.DataLoader(
                                                 eval_dataset, batch_size=args.eval_batch_size)
                    quantizer.eval_func = eval_func_for_nc
                    q_model = quantizer()
                    q_model.save(args.tuned_checkpoint)
                exit(0)

            if args.benchmark or args.accuracy_only:
                model = model_class.from_pretrained(checkpoint, mix_qkv=True)
                model.to(args.device)

                if args.int8:
                    from neural_compressor.utils.pytorch import load
                    new_model = load(
                        os.path.abspath(os.path.expanduser(args.tuned_checkpoint)), model)
                else:
                    new_model = model
                result, _ = evaluate(args, new_model, tokenizer, prefix=prefix)
                exit(0)

            if args.do_calibration:
                model = model_class.from_pretrained(checkpoint, mix_qkv=True)
                model.to(args.device)
                model.qconfig = default_per_channel_qconfig
                fallback_layers={}
                if args.model_name_or_path == "bert-base-uncased" and args.task_name == "mrpc":
                   fallback_layers = {"bert.encoder.layer.9.output.dense."}
                propagate_qconfig_(model)
                fallback_layer(model, layer_name = "", exculde_layers = fallback_layers)
                add_observer_(model)
                result, _ = evaluate(args, model, tokenizer, prefix=global_step, calibration = True)
                convert(model, inplace=True)
                quantized_model_path = args.task_name + "_quantized_model"
                if not os.path.exists(quantized_model_path):
                         os.makedirs(quantized_model_path)
                model.save_pretrained(quantized_model_path)
                print(model)
                result, _ = evaluate(args, model, tokenizer, prefix=prefix)
            if args.do_int8_inference:
               model = model_class.from_pretrained(checkpoint, mix_qkv=True)
               model.to(args.device)
               model.qconfig = default_per_channel_qconfig
               fallback_layers={}
               if args.model_name_or_path == "bert-base-uncased" and args.task_name == "mrpc":
                  fallback_layers =  {"bert.encoder.layer.9.output.dense."}
               propagate_qconfig_(model)
               fallback_layer(model, layer_name = "", exculde_layers = fallback_layers)
               add_observer_(model)
               convert(model, inplace=True)
               quantized_model_path = args.task_name + "_quantized_model"
               if not os.path.exists(quantized_model_path):
                       logger.error("please do calibrantion befor run int8 inference")
                       return 
               prepare(model, inplace = True)
               convert(model, inplace=True)
               model_bin_file = os.path.join(quantized_model_path,"pytorch_model.bin" )
               state_dict = torch.load(model_bin_file)
               model.load_state_dict(state_dict)
               result, _ = evaluate(args, model, tokenizer, prefix=prefix)

    return results


if __name__ == "__main__":
    main()
