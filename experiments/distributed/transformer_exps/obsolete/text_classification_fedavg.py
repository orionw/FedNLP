"""
An example of running fedavg experiments of fed-transformer models in FedNLP.
"""
import argparse
import logging
import os
import random
import socket
import sys

import psutil
import torch
import wandb

# this is a temporal import, we will refactor FedML as a package installation
sys.path.insert(0, os.path.abspath(os.path.join(os.getcwd(), "")))
sys.path.insert(0, os.path.abspath(os.path.join(os.getcwd(), "../../../../")))

import data_preprocessing.AGNews.data_loader
import data_preprocessing.SST_2.data_loader
import data_preprocessing.SemEval2010Task8.data_loader
import data_preprocessing.Sentiment140.data_loader
import data_preprocessing.news_20.data_loader

from FedML.fedml_api.distributed.fedavg.FedAvgAPI import FedML_init, FedML_FedAvg_distributed
from FedML.fedml_api.distributed.utils.gpu_mapping import mapping_processes_to_gpu_device_from_yaml_file
from data_preprocessing.base.utils import *
from model.fed_transformers.classification import ClassificationModel
from training.transformer_trainer import TransformerTrainer


def add_args(parser):
    """
    parser : argparse.ArgumentParser
    return a parser added with args required by fit
    """
    # Data related
    parser.add_argument(
        '--dataset', type=str, default='20news', metavar='N',
        help='dataset used for training')

    parser.add_argument(
        '--data_file', type=str,
        default='data/data_loaders/20news_data_loader.pkl',
        help='data pickle file')

    parser.add_argument(
        '--partition_file', type=str,
        default='data/partition/20news_partition.pkl',
        help='partition pickle file')

    parser.add_argument(
        '--partition_method', type=str, default='uniform', metavar='N',
        help='how to partition the dataset')

    # Model related
    parser.add_argument(
        '--model_type', type=str, default='distilbert', metavar='N',
        help='transformer model type')
    parser.add_argument(
        '--model_name', type=str, default='distilbert-base-uncased',
        metavar='N', help='transformer model name')
    parser.add_argument('--do_lower_case', type=bool, default=True, metavar='N',
                        help='transformer model name')

    # Learning related

    parser.add_argument('--train_batch_size', type=int, default=8, metavar='N',
                        help='input batch size for training (default: 8)')
    parser.add_argument('--eval_batch_size', type=int, default=8, metavar='N',
                        help='input batch size for evaluation (default: 8)')

    parser.add_argument('--max_seq_length', type=int, default=128, metavar='N',
                        help='maximum sequence length (default: 128)')

    parser.add_argument(
        '--learning_rate', type=float, default=1e-5, metavar='LR',
        help='learning rate (default: 1e-5)')
    parser.add_argument('--weight_decay', type=float, default=0, metavar='N',
                        help='L2 penalty')

    parser.add_argument('--epochs', type=int, default=3, metavar='EP',
                        help='how many epochs will be trained locally')
    parser.add_argument(
        '--gradient_accumulation_steps', type=int, default=1, metavar='EP',
        help='how many steps for accumulate the loss.')
    parser.add_argument('--n_gpu', type=int, default=1, metavar='EP',
                        help='how many gpus will be used ')
    parser.add_argument('--fp16', default=False, action="store_true",
                        help='if enable fp16 for training')
    parser.add_argument('--manual_seed', type=int, default=42, metavar='N',
                        help='random seed')

    # IO realted

    parser.add_argument('--output_dir', type=str, default="/tmp/", metavar='N',
                        help='path to save the trained results and ckpts')

    # FedAVG related

    parser.add_argument('--comm_round', type=int, default=10,
                        help='how many round of communications we shoud use')

    parser.add_argument('--is_mobile', type=int, default=0,
                        help='whether the program is running on the FedML-Mobile server side')

    parser.add_argument('--frequency_of_the_test', type=int, default=1,
                        help='the frequency of the algorithms')

    parser.add_argument('--ci', type=int, default=0,
                        help='CI')

    parser.add_argument('--client_num_in_total', type=int, default=1000, metavar='NN',
                        help='number of workers in a distributed cluster')

    parser.add_argument('--client_num_per_round', type=int, default=4, metavar='NN',
                        help='number of workers')

    parser.add_argument('--gpu_mapping_file', type=str, default="gpu_mapping.yaml",
                        help='the gpu utilization file for servers and clients. If there is no \
                    gpu_util_file, gpu will not be used.')

    parser.add_argument('--gpu_mapping_key', type=str, default="mapping_default",
                        help='the key in gpu utilization file')

    args = parser.parse_args()

    return args


def load_data(args, dataset):
    logging.info("Loading ClientDataLoader = %s" % dataset)
    if dataset == "20news":
        data_loader_class = data_preprocessing.news_20.data_loader
    elif dataset == "agnews":
        data_loader_class = data_preprocessing.AGNews.data_loader
    elif dataset == "semeval_2010_task8":
        data_loader_class = data_preprocessing.SemEval2010Task8.data_loader
    elif dataset == "sentiment140":
        data_loader_class = data_preprocessing.Sentiment140.data_loader
    elif dataset == "sst_2":
        data_loader_class = data_preprocessing.SST_2.data_loader
    else:
        raise Exception("No such dataset")

    server_data_loader = data_loader_class.ClientDataLoader(
        args.data_file, args.partition_file,
        partition_method=args.partition_method, tokenize=False, client_idx=None)

    client_data_loaders = []
    for client_index in range(server_data_loader.get_attributes()["n_clients"]):
        logging.info("loading client %d" % client_index)
        client_data_loader = data_loader_class.ClientDataLoader(
            args.data_file, args.partition_file,
            partition_method=args.partition_method, tokenize=False,
            client_idx=client_index)
        client_data_loaders.append(client_data_loader)

    data_attr = server_data_loader.get_attributes()
    train_data_local_num_dict = dict()
    train_data_local_dict = dict()
    test_data_local_dict = dict()
    for idx in range(data_attr["n_clients"]):
        logging.info("idx = %d" % idx)
        train_data_local_num_dict[idx] = client_data_loaders[idx].get_train_data_num(
        )
        train_data_local_dict[idx] = client_data_loaders[idx].get_train_batch_data(
            args.train_batch_size)
        test_data_local_dict[idx] = client_data_loaders[idx].get_test_batch_data(
            args.eval_batch_size)
    train_data_num = server_data_loader.get_train_data_num()
    test_data_num = server_data_loader.get_test_data_num()
    train_data_global = server_data_loader.get_train_batch_data()
    test_data_global = server_data_loader.get_test_batch_data()
    logging.info("Finished loading %s (END)" % dataset)
    return train_data_num, test_data_num, train_data_global, test_data_global, \
           train_data_local_num_dict, train_data_local_dict, test_data_local_dict, data_attr


def main(process_id, worker_number, args):
    # check "gpu_mapping.yaml" to see how to define the topology
    device = mapping_processes_to_gpu_device_from_yaml_file(process_id, worker_number, \
                                                            args.gpu_mapping_file, args.gpu_mapping_key)
    logging.info("process_id = %d, size = %d, device=%s" % (process_id, worker_number, str(device)))
    # Set Transformer logger.
    transformers_logger = logging.getLogger("transformers")
    transformers_logger.setLevel(logging.WARNING)

    # Loading full data (for centralized learning)
    train_data_num, test_data_num, train_data_global, test_data_global, \
    train_data_local_num_dict, train_data_local_dict, test_data_local_dict, \
    data_attr = load_data(args, args.dataset)

    labels_map = data_attr["target_vocab"]
    num_labels = len(labels_map)

    logging.info("num_clients = %d, train_data_num = %d, test_data_num = %d, num_labels = %d" % (
        data_attr["n_clients"], train_data_num, test_data_num, num_labels))

    # Transform data to DataFrame.
    # train_data = [(x, labels_map[y])
    #               for x, y in zip(train_data["X"], train_data["Y"])]
    # train_df = pd.DataFrame(train_data)

    # test_data = [(x, labels_map[y])
    #              for x, y in zip(test_data["X"], test_data["Y"])]
    # test_df = pd.DataFrame(test_data)

    # Create a ClassificationModel.
    transformer_model = ClassificationModel(
        args.model_type, args.model_name, num_labels=num_labels, labels_map=labels_map,
        args={"epochs": args.epochs,
              "learning_rate": args.learning_rate,
              "gradient_accumulation_steps": args.gradient_accumulation_steps,
              "do_lower_case": args.do_lower_case,
              "manual_seed": args.manual_seed,
              "reprocess_input_data": True,
              "overwrite_output_dir": True,
              "max_seq_length": args.max_seq_length,
              "train_batch_size": args.train_batch_size,
              "eval_batch_size": args.eval_batch_size,
              "dataloader_num_workers": 1,
              "thread_count": 1,
              "use_multiprocessing": False,
              "fp16": args.fp16,
              "n_gpu": args.n_gpu,
              "output_dir": args.output_dir,
              #   "wandb_project": "fednlp",
              })

    # Strat training.
    # model.train_model(train_df)

    model_trainer = TransformerTrainer(transformer_model=transformer_model, task_formulation="classification")


    # start FedAvg algorithm
    # for distributed algorithm, train_data_gloabl and test_data_global are required
    FedML_FedAvg_distributed(process_id, worker_number, device, comm,
                             transformer_model, train_data_num, train_data_global, test_data_global,
                             train_data_local_num_dict, train_data_local_dict, test_data_local_dict, args,
                             model_trainer)

    # # Evaluate the model
    # result, model_outputs, wrong_predictions = model.eval_model(
    #     test_df, acc=sklearn.metrics.accuracy_score)

    # print(result)


if __name__ == "__main__":
    # parse python script input parameters
    parser = argparse.ArgumentParser()
    args = add_args(parser)

    # Set manual seed.
    # Set the random seed. The np.random seed determines the dataset partition.
    # The torch_manual_seed determines the initial weight.
    # We fix these two, so that we can reproduce the result.
    random.seed(args.manual_seed)
    np.random.seed(args.manual_seed)
    torch.manual_seed(args.manual_seed)
    torch.cuda.manual_seed_all(args.manual_seed)

    # initialize distributed computing (MPI)
    comm, process_id, worker_number = FedML_init()

    # customize the log format
    logging.basicConfig(level=logging.INFO,
                        format='%(process)s %(asctime)s.%(msecs)03d - {%(module)s.py (%(lineno)d)} - %(funcName)s(): %(message)s',
                        datefmt='%Y-%m-%d,%H:%M:%S')
    logging.info(args)

    hostname = socket.gethostname()
    logging.info("#############process ID = " + str(process_id) +
                 ", host name = " + hostname + "########" +
                 ", process ID = " + str(os.getpid()) +
                 ", process Name = " + str(psutil.Process(os.getpid())))

    logging.info("process_id = %d, size = %d" % (process_id, worker_number))

    if process_id == 0:
        # initialize the wandb machine learning experimental tracking platform (https://wandb.ai/automl/fednlp).
        wandb.init(
            project="fednlp", entity="wellerorion", name="FedNLP-FedAVG-Transformer" +
                                                    "-TC-" + str(args.dataset) + "-" + str(args.model_name),
            config=args)

    # Start training.
    main(process_id, worker_number, args)
