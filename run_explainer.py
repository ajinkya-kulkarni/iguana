"""run_explainer.py

Get the node and feature explanations. Node, feature and wsi-level explanations must be run individually.

Usage:
  run_explainer.py [--gpu=<id>] [--fold=<n>] [--node] [--feature] [--wsi]
  run_explainer.py (-h | --help)
  run_explainer.py --version

Options:
  -h --help         Show this string.
  --version         Show version.
  --gpu=<id>        Comma separated GPU list. [default: 0]
  --fold=<n>        Fold number of model to process with. [default: 1]
  --node            Whether to compute node explanation         
  --feature         Whether to compute feature explanation
  --wsi             Whether to compute wsi explanation - must have performed node and feature explanation!
"""

import os
import sys
import numpy as np
import joblib
import glob
import logging
from docopt import docopt
from importlib import import_module
from scipy.stats import percentileofscore
from datetime import datetime

import torch
from torch_geometric.nn import GNNExplainer
from torch_geometric.loader import DataLoader

from captum.attr import Saliency, IntegratedGradients

from explainer.utils import to_captum
from misc.utils import rm_n_mkdir
from dataloader.graph_loader import FileLoader


def score_to_percentile(scores):
    # convert to probability
    mask = []
    for score_single in scores:
        score_single = percentileofscore(scores, score_single)
        score_single /= 100
        mask.append(score_single)
    return np.array(mask)
class Explainer(object):
    def __init__(
        self, 
        model_name, 
        explainer_method, 
        run_args,
        ckpt_path, 
        stats_path, 
        output_path, 
        k,
        feat_agg,
        norm="standard", 
        data_clean="std"
        ):
        
        if not os.path.exists(output_path):
            rm_n_mkdir(output_path)

        self.model_name = model_name
        self.ckpt_path = ckpt_path
        self.output_path = output_path
        self.node_explainer_method = explainer_method["node"]
        self.feat_explainer_method = explainer_method["feats"]
        self.run_explain_node = run_args[0]
        self.run_explain_feats = run_args[1]
        self.run_explain_graph = run_args[2]
        self.k = k
        self.feats_agg = feats_agg
        self.__load_model()

        self.output_path_node = f"{output_path}/node_explain/{self.node_explainer_method}"
        self.output_path_feats = f"{output_path}/feats_explain/{self.feat_explainer_method}"

        if self.run_explain_node:
            if not os.path.exists(f"{output_path}/node_explain"):
                rm_n_mkdir(f"{output_path}/node_explain")
            if not os.path.exists(self.output_path_node):
                rm_n_mkdir(self.output_path_node)
                
        if self.run_explain_feats:
            if not os.path.exists(f"{output_path}/feats_explain"):
                rm_n_mkdir(f"{output_path}/feats_explain")
            if not os.path.exists(self.output_path_feats):
                rm_n_mkdir(self.output_path_feats)

        mean = np.load(f"{stats_path}/mean.npy")
        median = np.load(f"{stats_path}/median.npy")
        std = np.load(f"{stats_path}/std.npy")
        perc_25 = np.load(f"{stats_path}/perc_25.npy")
        perc_75 = np.load(f"{stats_path}/perc_75.npy")

        self.feat_stats = [mean, median, std, perc_25, perc_75]
        self.norm = norm
        self.data_clean = data_clean
    
    def __load_model(self):
        """Create the model, load the checkpoint and define
        associated run steps to process each data batch

        """
        model_desc = import_module('models.net_desc')
        model_creator = getattr(model_desc, 'create_model')

        # TODO: deal with parsing multi level model desc
        self.model = model_creator(
            model_name=self.model_name, 
            nr_features=25, 
            return_prob=True
            )
        self.model = self.model.to('cuda')
        saved_state_dict = torch.load(self.ckpt_path)
        self.model.load_state_dict(saved_state_dict['desc'], strict=True)

        self.model2 = model_creator(
            model_name=self.model_name, 
            nr_features=25, 
            return_prob=False
            )
        self.model2 = self.model2.to('cuda')
        self.model2.load_state_dict(saved_state_dict['desc'], strict=True)
        run_desc = import_module('models.run_desc')
        self.run_step = lambda input_batch: getattr(
            run_desc, 'infer_step')(input_batch, self.model2
            )

    def run(self, data_list):
        """Run Explainer."""

        # get running list of top features and feature importances
        top_feats_list = []
        top_imports_list = []
        wsi_list = []
        # run one file at a time- otherwise pytorch geometric combines multiple WSIs into single graph!
        for file_idx, filename in enumerate(data_list):
            wsi_name = os.path.basename(filename)
            wsi_name = wsi_name[:-4]
            wsi_list.append(wsi_name)

            graph_dataset = FileLoader([filename], self.feat_stats, self.norm, self.data_clean)
    
            dataloader = DataLoader(
                graph_dataset,
                num_workers=1,
                batch_size=1,
                shuffle=False,
                drop_last=False,
            )
            graph_data = next(iter(dataloader))
             
            edge_index = graph_data.edge_index
            feats = graph_data.x
            # global_feats = graph_data.global_feats
            batch = graph_data.batch
            slide_ids = graph_data.wsi_info[0][:, 1]

            edge_index = edge_index.to("cuda").type(torch.long)
            feats = feats.to("cuda").type(torch.float32)
            # global_feats = global_feats.to("cuda").type(torch.float32)
            batch = batch.to("cuda").type(torch.int64)

            if self.run_explain_node:
                #* get node explanation
                if self.node_explainer_method == 'attention':
                    # node scores from global attention pooling
                    out_dict = self.run_step(graph_data)
                    node_scores = out_dict["node_scores"].cpu().detach().numpy()
                    node_scores = np.squeeze(node_scores)
                    # convert to probability
                    node_mask = score_to_percentile(node_scores)
                elif self.node_explainer_method == 'gnnexplainer':
                    # feat mask types: `individual_feature`, `feature` or `scalar`
                    explainer = GNNExplainer(self.model, epochs=450, return_type='prob', feat_mask_type="scalar", allow_edge_mask=True)
                    node_mask, _ = explainer.explain_graph(feats, edge_index)
                    node_mask = np.abs(node_mask.cpu().detach().numpy()) # already a probability
                elif self.node_explainer_method == 'ig':
                    captum_model = to_captum(self.model, mask_type="node")
                    ig = IntegratedGradients(captum_model)
                    ig_attr = ig.attribute(
                        feats.unsqueeze(0),
                        target=1, # positive class
                        additional_forward_args=(batch, edge_index),
                        internal_batch_size=1,
                    )
                    feature_mask = np.abs(ig_attr[0].cpu().detach().numpy())
                    node_scores = np.mean(feature_mask, -1) # compute mean across feature dimensions
                    # convert to probability
                    node_mask = score_to_percentile(node_scores)
                elif self.node_explainer_method == 'gradients':
                    captum_model = to_captum(self.model, mask_type="node")
                    saliency = Saliency(captum_model)
                    ig_attr = saliency.attribute(
                        feats.unsqueeze(0),
                        target=1, # positive class
                        additional_forward_args=(batch, edge_index),
                        )
                    feature_mask = np.abs(ig_attr[0].cpu().detach().numpy())
                    node_scores = np.mean(feature_mask, -1) # compute mean across feature dimensions
                    # convert to probability
                    node_mask = score_to_percentile(node_scores)
                elif self.node_explainer_method == 'random':
                    nr_nodes =  feats.shape[0]
                    node_mask = np.random.rand(nr_nodes)
                
                # save node explanations!
                output_node_exp = {"obj_id": slide_ids.tolist(), "node_exp": node_mask}
                joblib.dump(output_node_exp, f"{self.output_path_node}/{wsi_name}.dat")

            if self.run_explain_feats:
                #* get feature explanation
                if self.feat_explainer_method == 'ig':
                    captum_model = to_captum(self.model, mask_type="node")
                    ig = IntegratedGradients(captum_model)
                    ig_attr = ig.attribute(
                        feats.unsqueeze(0),
                        target=1, # positive class
                        additional_forward_args=(batch, edge_index),
                        internal_batch_size=1,
                    )
                    feature_mask = np.abs(ig_attr[0].cpu().detach().numpy())
                elif self.feat_explainer_method == 'gradients':
                    captum_model = to_captum(self.model, mask_type="node")
                    saliency = Saliency(captum_model)
                    ig_attr = saliency.attribute(
                        feats.unsqueeze(0),
                        target=1, # positive class
                        additional_forward_args=(batch, edge_index),
                        )
                    feature_mask = np.abs(ig_attr[0].cpu().detach().numpy())
                elif self.feat_explainer_method == 'gnnexplainer':
                    # feat mask types: `individual_feature`, `feature` or `scalar`
                    explainer = GNNExplainer(self.model, epochs=500, return_type='prob', feat_mask_type="individual_feature", allow_edge_mask=True)
                    feature_mask, _ = explainer.explain_graph(feats, edge_index)
                    feature_mask = np.abs(feature_mask.cpu().detach().numpy())    
                elif self.node_explainer_method == 'random':
                    nr_nodes =  feats.shape[0]
                    feature_mask = np.random.rand(nr_nodes, 26)
                    feat_sum = np.sum(feature_mask, -1)
                    feat_sum = np.expand_dims(feat_sum, -1)
                    feature_mask = feature_mask / feat_sum
                
                # save feat explanations!
                # also save original features for easy retrieval
                output_feat_exp = {"obj_id": slide_ids.tolist(), "feat_exp": feature_mask, "feats": feats.cpu().detach().numpy()}
                joblib.dump(output_feat_exp, f"{self.output_path_feats}/{wsi_name}.dat")            

            if self.run_explain_graph:
                #* load in the node and feature explanations!
                node_mask = joblib.load(f"{self.output_path_node}/{wsi_name}.dat")["node_exp"]
                feature_mask = joblib.load(f"{self.output_path_feats}/{wsi_name}.dat")["feat_exp"]
    
                # get top features and importances
                sorted_ids = np.argsort(node_mask)
                k = self.k
                if sorted_ids.shape[0] < k:
                    k = sorted_ids.shape[0]
                max_ids = sorted_ids[-k:]
                features_list = []
                for idx in range(k):
                    id_sel = max_ids[idx]
                    features_list.append(feats[id_sel].cpu().detach().numpy())
                mean_top_features = np.mean(np.array(features_list), axis=0)
                top_feats_list.append(mean_top_features)

                if self.feats_agg == 'weighted_avg':
                    # node scores from global attention pooling
                    out_dict = self.run_step(graph_data)
                    node_scores = out_dict["node_scores"].cpu().detach().numpy()
                    weighted_feature_mask = feature_mask * node_scores # multiply by relative importance
                    weighted_avg_feature_mask = np.sum(weighted_feature_mask, 0) # perform sum of weighted importances
                    # add to list
                    top_imports_list.append(weighted_avg_feature_mask)

                elif self.feats_agg == 'gnnexplainer':
                    explainer = GNNExplainer(self.model, epochs=500, return_type='prob', feat_mask_type="feature", allow_edge_mask=False)
                    global_feature_mask, _ = explainer.explain_graph(feats, edge_index)
                    global_feature_mask = np.abs(global_feature_mask.cpu().detach().numpy())    
                    # add to list
                    top_imports_list.append(global_feature_mask)
                
                elif self.feats_agg == 'top_avg_gnnexplainer':
                    imports_list = []
                    for idx in range(k):
                        id_sel = max_ids[idx]
                        imports_list.append(feature_mask[id_sel])
                    mean_top_imports = np.mean(np.array(imports_list), axis=0)
                    top_imports_list.append(mean_top_imports)

                elif self.feats_agg == 'top_avg_attention':
                    # sort the attention scores
                    out_dict = self.run_step(graph_data)
                    node_scores = np.squeeze(out_dict["node_scores"].cpu().detach().numpy())
                    sorted_ids = np.argsort(node_scores)
                    k = self.k
                    if sorted_ids.shape[0] < k:
                        k = sorted_ids.shape[0]
                    max_ids = sorted_ids[-k:]
                    
                    imports_list = []
                    for idx in range(k):
                        id_sel = max_ids[idx]
                        imports_list.append(feature_mask[id_sel])
                    mean_top_imports = np.mean(np.array(imports_list), axis=0)
                    top_imports_list.append(mean_top_imports)
 
            sys.stdout.write(f"\rProcessed file {file_idx+1}/{len(data_list)} ")
            sys.stdout.flush()
        
        return wsi_list, top_feats_list, top_imports_list


if __name__ == "__main__":
    args = docopt(__doc__, version="IGUANA explain v1.0")
    print(args)
    os.environ["CUDA_VISIBLE_DEVICES"] = args["--gpu"]

    node_explainer_method = "gnnexplainer" # choose from `attention`, `ig`, `gradients`, `gnnexplainer` or `random`
    feat_explainer_method = "gnnexplainer" # choose from `ig` `gradients` or `gnnexplainer`  or `random`
    
    run_explain_node = args['--node']
    run_explain_feats = args['--feature']
    run_explain_graph = args['--wsi'] # need to get node and feat explanations first! will read already processed files
    
    k = 10 # number of top objects to consider for global aggregation!
    feats_agg = "top_avg_gnnexplainer" # `weighted_avg` (weighted average of local feats), `gnnexplainer`, `top_avg_attention` or `top_avg_gnnexplainer`

    explainer_method = {"node": node_explainer_method, "feats": feat_explainer_method}
    run_args = [run_explain_node, run_explain_feats, run_explain_graph]

    dataset_name = "uhcw"
    model_name = "pna"
    ckpt_root = "/root/lsf_workspace/iguana_data/weights/"
    fold = int(args['--fold']) # if uhcw, process relevant test set - otherwise process all data with the model trained on this fold

    # modify below if wish to only get the explanation for a single WSI
    single_slide = False
    wsi_name_list = ["H17-61904_C1RIBH17-61904C11RIB_1"]
    
    output_path = "output_test/"
    data_root = f"/root/lsf_workspace/iguana_data/graph_data/{dataset_name}/"
    if dataset_name == "uhcw" and not single_slide:
        data_root = f"{data_root}/test{fold}/"
    stats_path = "/root/lsf_workspace/iguana_data/stats/"
    
    wsi_list = []
    top_feats_list = []
    top_imports_list = []

    if single_slide:
        for wsi_name in wsi_name_list:
            if os.path.exists(f"/{data_root}/test1/{wsi_name}.dat"):
                fold = 1
            elif os.path.exists(f"/{data_root}/test2/{wsi_name}.dat"):
                fold = 2
            elif os.path.exists(f"/{data_root}/test3/{wsi_name}.dat"):
                fold = 3
            data_list = [f"/{data_root}/test{fold}/{wsi_name}.dat"]
            ckpt_path = f"{ckpt_root}/iguana_fold{fold}.tar"

            xplainer = Explainer(model_name, explainer_method, run_args, ckpt_path, stats_path, output_path, k, feats_agg)
            wsi_list_, top_feats_list_, top_imports_list_ = xplainer.run(data_list)
            wsi_list.extend(wsi_list_)
            top_feats_list.extend(top_feats_list_)
            top_imports_list.extend(top_imports_list_)
        
    else:
        data_list = glob.glob(f"{data_root}/*.dat")
        ckpt_path = f"{ckpt_root}/iguana_fold{fold}.tar"

        xplainer = Explainer(model_name, explainer_method, run_args, ckpt_path, stats_path, output_path, k, feats_agg)
        wsi_list_, top_feats_list_, top_imports_list_ = xplainer.run(data_list)
        wsi_list.extend(wsi_list_)
        top_feats_list.extend(top_feats_list_)
        top_imports_list.extend(top_imports_list_)
    
    if run_explain_graph:
        top_feats_dict = {"wsi": wsi_list, "top_features": np.array(top_feats_list), "top_importances": np.array(top_imports_list)}
        if feats_agg == "weighted_avg":
            output_path_topfeats = f"{output_path}/top_feats_explain/{feats_agg}_{feat_explainer_method}"
        elif feats_agg == "gnnexplainer":
            output_path_topfeats = f"{output_path}/top_feats_explain/{feats_agg}"
        elif feats_agg == "top_avg_gnnexplainer":
            output_path_topfeats = f"{output_path}/top_feats_explain/{feats_agg}_{k}"
        elif feats_agg == "top_avg_attention":
            output_path_topfeats = f"{output_path}/top_feats_explain/{feats_agg}_{k}"
        if not os.path.exists(output_path_topfeats):
            rm_n_mkdir(output_path_topfeats)
        joblib.dump(top_feats_dict, f"{output_path_topfeats}/top_features.dat")
    print('DONE!')
