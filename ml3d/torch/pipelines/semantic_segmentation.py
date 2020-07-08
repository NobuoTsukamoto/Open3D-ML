#coding: future_fstrings
import torch, pickle
import torch.nn as nn
import helper_torch_util 
import numpy as np
from pprint import pprint
import time
from tqdm import tqdm
from sklearn.neighbors import KDTree
from torch.utils.tensorboard import SummaryWriter
from torch.utils.data import Dataset, IterableDataset, DataLoader, Sampler, BatchSampler
from os import makedirs
from os.path import exists, join, isfile, dirname, abspath
from ml3d.datasets.semantickitti import DataProcessing

import yaml


def log_out(out_str, f_out):
    f_out.write(out_str + '\n')
    f_out.flush()
    print(out_str)

def intersection_over_union(scores, labels):
    r"""
        Compute the per-class IoU and the mean IoU # TODO: complete doc

        Parameters
        ----------
        scores: torch.FloatTensor, shape (B?, C, N)
            raw scores for each class
        labels: torch.LongTensor, shape (B?, N)
            ground truth labels

        Returns
        -------
        list of floats of length num_classes+1 (last item is mIoU)
    """
    num_classes = scores.size(-2) # we use -2 instead of 1 to enable arbitrary batch dimensions

    predictions = torch.max(scores, dim=-2).indices

    ious = []

    for label in range(num_classes):
        pred_mask = predictions == label
        labels_mask = labels == label
        iou = (pred_mask & labels_mask).float().sum() / (pred_mask | labels_mask).float().sum()
        ious.append(iou.cpu().item())
    ious.append(np.nanmean(ious))
    return ious


def accuracy(scores, labels):
    r"""
        Compute the per-class accuracies and the overall accuracy # TODO: complete doc

        Parameters
        ----------
        scores: torch.FloatTensor, shape (B?, C, N)
            raw scores for each class
        labels: torch.LongTensor, shape (B?, N)
            ground truth labels

        Returns
        -------
        list of floats of length num_classes+1 (last item is overall accuracy)
    """
    num_classes = scores.size(-2) # we use -2 instead of 1 to enable arbitrary batch dimensions

    predictions = torch.max(scores, dim=-2).indices

    accuracies = []

    accuracy_mask = predictions == labels
    for label in range(num_classes):
        label_mask = labels == label
        per_class_accuracy = (accuracy_mask & label_mask).float().sum()
        per_class_accuracy /= label_mask.float().sum()
        accuracies.append(per_class_accuracy.cpu().item())
    # overall accuracy
    accuracies.append(accuracy_mask.float().mean().cpu().item())
    return accuracies


class SemanticSegmentation():
    def __init__(self, model, dataset, cfg):
        '''
        flat_inputs = dataset.flat_inputs
        self.config = config
        # Path of the result folder
        if self.config.saving:
            if self.config.saving_path is None:
                self.saving_path = time.strftime('results/Log_%Y-%m-%d_%H-%M-%S', time.gmtime())
            else:
                self.saving_path = self.config.saving_path
            makedirs(self.saving_path) if not exists(self.saving_path) else None
        '''
        
        self.model      = model
        self.dataset    = dataset
        self.config     = cfg

    def run_inference(self, points, device):
        cfg = self.config
        grid_size   = cfg.grid_size

        input_inference = self.preprocess_inference(points, device)
        self.eval()
        scores = self(input_inference)

        pred = torch.max(scores, dim=-2).indices
        pred   = pred.cpu().data.numpy()
        return pred


    def run_test(self, device):
        #self.device = device
        model   = self.model
        dataset = self.dataset
        cfg     = self.config
        model.to(device)

        Log_file = open('log_test_' + dataset.name + '.txt', 'a')
        test_sampler = dataset.get_ActiveLearningSampler('test')
        test_loader = DataLoader(test_sampler, batch_size=cfg.val_batch_size)
        test_probs = [np.zeros(shape=[len(l), self.config.num_classes], dtype=np.float16)
                           for l in dataset.possibility]


        test_smooth = 0.98
        epoch_ind   = 0
        model.eval()

        while True:
            for batch_data in tqdm(test_loader, desc='test', leave=False):
                # loader: point_clout, label
                inputs          = model.preprocess(batch_data, device) 
                result_torch    = model(inputs)
                result_torch    = torch.reshape(result_torch,
                                                    (-1, cfg.num_classes))

                m_softmax       = nn.Softmax(dim=-1)
                result_torch    = m_softmax(result_torch)
                stacked_probs   = result_torch.cpu().data.numpy()

                stacked_probs = np.reshape(stacked_probs, [cfg.val_batch_size,
                                                           cfg.num_points,
                                                           cfg.num_classes])
              
                point_inds  = inputs['input_inds']
                cloud_inds  = inputs['cloud_inds']

                for j in range(np.shape(stacked_probs)[0]):
                    probs = stacked_probs[j, :, :]
                    inds = point_inds[j, :]
                    c_i = cloud_inds[j][0]
                    test_probs[c_i][inds] = \
                                test_smooth * test_probs[c_i][inds] + \
                                (1 - test_smooth) * probs
          

            new_min = np.min(dataset.min_possibility)
            log_out('Epoch {:3d}, end. Min possibility = {:.1f}'.format(epoch_ind, new_min), Log_file)
           
            if np.min(dataset.min_possibility) > 0.5:  # 0.5
                print('\nReproject Vote #{:d}'.format(int(np.floor(new_min))))
                dataset.save_test_result(test_probs)
                log_out(str(cfg.test_split_number) + ' finished', Log_file)
                return
          
            epoch_ind += 1
            continue


    def run_train(self, device):
        #self.device = device
        model   = self.model
        dataset = self.dataset
        cfg     = self.config
        model.to(device)        
        model.train()

        n_samples       = torch.tensor(cfg.class_weights, 
                            dtype=torch.float, device=device)
        ratio_samples   = n_samples / n_samples.sum()
        weights         = 1 / (ratio_samples + 0.02)

        criterion = nn.CrossEntropyLoss(weight=weights)

        train_sampler   = dataset.get_ActiveLearningSampler('training')
        train_loader    = DataLoader(train_sampler, 
                                     batch_size=cfg.val_batch_size)
        optimizer = torch.optim.Adam(model.parameters(), lr=cfg.adam_lr)
        scheduler = torch.optim.lr_scheduler.ExponentialLR(optimizer, 
                                                           cfg.scheduler_gamma)

        first_epoch = 1
        logs_dir    = cfg.logs_dir
        '''
        if args.load:
            path = max(list((args.logs_dir / args.load).glob('*.pth')))
            print(f'Loading {path}...')
            checkpoint = torch.load(path)
            first_epoch = checkpoint['epoch']+1
            model.load_state_dict(checkpoint['model_state_dict'])
            optimizer.load_state_dict(checkpoint['optimizer_state_dict'])
            scheduler.load_state_dict(checkpoint['scheduler_state_dict'])
        '''

        with SummaryWriter(logs_dir) as writer:
            for epoch in range(first_epoch, cfg.max_epoch+1):
                print(f'=== EPOCH {epoch:d}/{cfg.max_epoch:d} ===')
                # metrics
                losses      = []
                accuracies  = []
                ious        = []
                step        = 0

                for batch_data in tqdm(train_loader, desc='Training', leave=False):

                    
                    inputs = model.preprocess(batch_data, device) 
                    scores = model(inputs)

                    labels = batch_data[1] 
                    scores, labels = self.filter_valid(scores, labels, device)
                    logp = torch.distributions.utils.probs_to_logits(scores, 
                                                            is_binary=False)
                    loss = criterion(logp, labels)
                    acc  = accuracy(scores, labels)

                    optimizer.zero_grad()
                    loss.backward()
                    optimizer.step()

                    step = step + 1
                    if (step % 50==0):
                        print(loss)
                        print(acc[-1])

                    losses.append(loss.cpu().item())
                    #accuracies.append(accuracy(scores, labels))
                    #ious.append(intersection_over_union(scores, labels))


    def filter_valid(self, scores, labels, device):
        valid_scores = scores.reshape(-1, self.config.num_classes)
        valid_labels = labels.reshape(-1).to(device)
                
        ignored_bool = torch.zeros_like(valid_labels, dtype=torch.bool)
        for ign_label in self.config.ignored_label_inds:
            ignored_bool = torch.logical_or(ignored_bool, 
                            torch.eq(valid_labels, ign_label))
           
        valid_idx = torch.where(
            torch.logical_not(ignored_bool))[0].to(device)

        valid_scores = torch.gather(valid_scores, 0, 
            valid_idx.unsqueeze(-1).expand(-1, self.config.num_classes))
        valid_labels = torch.gather(valid_labels, 0, valid_idx)

        # Reduce label values in the range of logit shape
        reducing_list = torch.arange(0, 
                        self.config.num_classes, dtype=torch.int64)
        inserted_value = torch.zeros([1], dtype=torch.int64)
        
        for ign_label in self.config.ignored_label_inds:
            reducing_list = torch.cat([reducing_list[:ign_label],
                     inserted_value, reducing_list[ign_label:]], 0)
        valid_labels = torch.gather(reducing_list.to(device), 
                                        0, valid_labels)

        valid_labels = valid_labels.unsqueeze(0)
        valid_scores = valid_scores.unsqueeze(0).transpose(-2,-1)


        return valid_scores, valid_labels