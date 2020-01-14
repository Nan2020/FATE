from arch.api.utils import log_utils

import functools
import copy
import arch
from arch.api import session
from federatedml.protobuf.generated.boosting_tree_model_meta_pb2 import CriterionMeta
from federatedml.protobuf.generated.boosting_tree_model_meta_pb2 import DecisionTreeModelMeta
from federatedml.protobuf.generated.boosting_tree_model_param_pb2 import DecisionTreeModelParam
from federatedml.transfer_variable.transfer_class.homo_decision_tree_transfer_variable import \
    HomoDecisionTreeTransferVariable
from federatedml.util import consts
from federatedml.tree import FeatureHistogram
from federatedml.tree import DecisionTree
from federatedml.tree import Splitter
from federatedml.tree import Node
from federatedml.tree.feature_histogram import HistogramBag
from federatedml.feature.fate_element_type import NoneType
from federatedml.framework.homo.procedure import aggregator

from arch.api.table.eggroll.table_impl import DTable
from federatedml.feature.instance import Instance
from federatedml.param import DecisionTreeParam

import numpy as np
from typing import List,Dict,Tuple
from federatedml.tree.splitter import SplitInfo
import pandas as pd

from federatedml.tree.homo_secureboosting_aggregator import SecureBoostClientAggregator

# LOGGER = log_utils.getLogger()

class LocalTestLogger(object):

    def __init__(self):
        pass

    def debug(self,*args):
        print(*args)

    def info(self,*args):
        self.debug(*args)

LOGGER = LocalTestLogger()

class HomoDecisionTreeClient(DecisionTree):

    def __init__(self,tree_param:DecisionTreeParam,binned_data:DTable,bin_split_points:np.array,bin_sparse_point,g_h:DTable
                 ,valid_feature:dict,tree_idx:int,role:str):

        """
        Parameters
        ----------
        tree_param: decision tree parameter object
        binned_data binned: data instance
        bin_split_points: data split points
        bin_sparse_point: sparse data point
        g_h computed: g val and h val of instances
        valid_feature: dict points out valid features {valid:true,invalid:false}
        tree_idx: current tree index
        role: host or guest
        """

        super(HomoDecisionTreeClient, self).__init__(tree_param)
        self.splitter = Splitter(self.criterion_method, self.criterion_params, self.min_impurity_split,
                                 self.min_sample_split, self.min_leaf_node)
        self.binned_data = binned_data
        self.g_h = g_h
        self.bin_split_points = bin_split_points
        self.bin_sparse_points = bin_sparse_point
        self.tree_idx = tree_idx

        self.transfer_inst = HomoDecisionTreeTransferVariable()

        """
        initializing here
        """
        self.valid_features = valid_feature

        self.tree_node = []  # start from root node
        self.tree_node_num = 0
        self.cur_layer_node = []

        self.runtime_idx = 0
        self.sitename = consts.GUEST
        self.feature_importances_ = {}

        self.inst2node_idx = None

        # record weights of samples
        self.sample_weights = None

        # secure aggregator, class SecureBoostClientAggregator
        assert role == consts.HOST or role == consts.GUEST
        self.role = role
        # self.aggregator = SecureBoostClientAggregator(role=self.role,transfer_variable=self.transfer_inst)

        # for testing
        self.split_history = []

    def set_flowid(self, flowid=0):
        LOGGER.info("set flowid, flowid is {}".format(flowid))
        self.transfer_inst.set_flowid(flowid)

    def get_grad_hess_sum(self, grad_and_hess_table) -> Tuple[DTable,DTable]:
        LOGGER.info("calculate the sum of grad and hess")
        grad, hess = grad_and_hess_table.reduce(
            lambda value1, value2: (value1[0] + value2[0], value1[1] + value2[1]))
        return grad, hess

    def update_feature_importance(self,split_info:List[SplitInfo]):

        for splitinfo in split_info:

            if self.feature_importance_type == "split":
                inc = 1
            elif self.feature_importance_type == "gain":
                inc = splitinfo.gain
            else:
                raise ValueError("feature importance type {} not support yet".format(self.feature_importance_type))

            sitename = splitinfo.sitename
            fid = splitinfo.best_fid

            if (sitename, fid) not in self.feature_importances_:
                self.feature_importances_[(sitename, fid)] = 0

            self.feature_importances_[(sitename, fid)] += inc

    def sync_local_node_histogram(self, acc_histogram:List[HistogramBag], suffix):
        # sending local histogram
        self.transfer_inst.local_histogram.remote(acc_histogram,role=consts.ARBITER,idx=-1,suffix=suffix)
        # LOGGER.debug(acc_histogram[0])
        # self.aggregator.send_histogram(acc_histogram,suffix=suffix)
        LOGGER.debug('local histogram sent at layer {}'.format(suffix[0]))
        # best_splits = self.splitter.find_split(acc_histogram,self.valid_features,self.binned_data._partitions,
        #                                        self.sitename,self.use_missing,self.zero_as_missing)

    def get_local_histogram(self,node_map,g_h,table_with_assign,split_points,sparse_point,
                            valid_feature):
        LOGGER.info("start to get node histograms")
        histograms = FeatureHistogram.calculate_histogram(
            table_with_assign, g_h,
            split_points, sparse_point,
            valid_feature, node_map,
            self.use_missing, self.zero_as_missing)
        LOGGER.info("begin to accumulate histograms")
        acc_histograms = FeatureHistogram.accumulate_histogram(histograms)
        return acc_histograms

    def update_tree(self,cur_to_split:List[Node],split_info:List[SplitInfo]):
        """
        update current tree structure
        ----------
        split_info
        """
        LOGGER.debug('updating tree_node, cur layer has {} node,'.format(len(cur_to_split)))
        next_layer_node = []
        assert len(cur_to_split) == len(split_info)
        for idx in range(len(cur_to_split)):
            sum_grad = cur_to_split[idx].sum_grad
            sum_hess = cur_to_split[idx].sum_hess
            if split_info[idx].best_fid is None or split_info[idx].gain <= self.min_impurity_split + consts.FLOAT_ZERO:
                cur_to_split[idx].is_leaf = True
                self.tree_node.append(cur_to_split[idx])
                continue

            cur_to_split[idx].fid = split_info[idx].best_fid
            cur_to_split[idx].bid = split_info[idx].best_bid

            l_id,r_id = self.tree_node_num + 1,self.tree_node_num + 2
            cur_to_split[idx].left_nodeid,cur_to_split[idx].right_nodeid = l_id,r_id
            self.tree_node_num += 2

            l_g,l_h = split_info[idx].sum_grad,split_info[idx].sum_hess

            # create new left node and new right node
            left_node = Node(id=l_id,
                             sitename=self.sitename,
                             sum_grad=l_g,
                             sum_hess=l_h,
                             weight=self.splitter.node_weight(l_g, l_h))
            right_node = Node(id=r_id,
                              sitename=self.sitename,
                              sum_grad=sum_grad - l_g,
                              sum_hess=sum_hess - l_h,
                              weight=self.splitter.node_weight(sum_grad - l_g,sum_hess - l_h))

            next_layer_node.append(left_node)
            print('append left,cur tree has {} node'.format(len(self.tree_node)))
            next_layer_node.append(right_node)
            print('append right,cur tree has {} node'.format(len(self.tree_node)))
            self.tree_node.append(cur_to_split[idx])

        return next_layer_node

    def convert_bin_to_val(self):
        """
        convert current bid in tree nodes to real value
        """
        for node in self.tree_node:
            if not node.is_leaf:
                node.bid = self.bin_split_points[node.fid][node.bid]

    def assign_instance_to_root_node(self,binned_data:DTable,root_node_id):
        return binned_data.mapValues(lambda inst:(1,root_node_id))

    def assign_a_instance(self,row,tree:List[Node],bin_sparse_point,use_missing,use_zero_as_missing):

        leaf_status,nodeid = row[1]
        node = tree[nodeid]
        if node.is_leaf:
            return node.weight

        fid = node.fid
        bid = node.bid

        missing_dir = node.missing_dir

        missing_val = False
        if use_zero_as_missing:
            if row[0].features.get_data(fid, None) is None or \
                    row[0].features.get_data(fid) == NoneType():
                missing_val = True
        elif use_missing and row[0].features.get_data(fid) == NoneType():
            missing_val = True

        if missing_val:
            if missing_dir == 1:
                return 1, tree[nodeid].right_nodeid
            else:
                return 1, tree[nodeid].left_nodeid
        else:
            if row[0].features.get_data(fid, bin_sparse_point[fid]) <= bid:
                return 1, tree[nodeid].left_nodeid
            else:
                return 1, tree[nodeid].right_nodeid


    def assign_instance_to_new_node(self, table_with_assignment:DTable, tree_node:List[Node]):

        LOGGER.debug('re-assign instance to new nodes')
        assign_method = functools.partial(self.assign_a_instance,tree=tree_node,bin_sparse_point=
                                          self.bin_sparse_points,use_missing=self.use_missing,use_zero_as_missing
                                          =self.zero_as_missing)
        assign_result = table_with_assignment.mapValues(assign_method)
        leaf_val = assign_result.filter(lambda key, value: isinstance(value, tuple) is False)

        assign_result = assign_result.subtractByKey(leaf_val)

        return assign_result,leaf_val

    def get_node_sample_weights(self, inst2node:DTable, tree_node:List[Node]):
        """
        get samples' weights which correspond to its node assignment
        """
        func = lambda inst,tree_node:tree_node[inst[1]].weight
        func = functools.partial(func,tree_node=tree_node)
        return inst2node.mapValues(func)

    def sync_tree(self,):
        pass

    def sync_cur_layer_node_num(self,node_num,suffix):
        self.transfer_inst.cur_layer_node_num.remote(node_num,role=consts.ARBITER,idx=-1,suffix=suffix)

    def sync_best_splits(self,suffix) -> List[SplitInfo]:
        best_splits = self.transfer_inst.best_split_points.get(idx=0,suffix=suffix)
        return best_splits

    def fit(self):
        """
        start to fit
        """
        LOGGER.info('begin to fit homo decision tree')

        g_sum,h_sum = self.get_grad_hess_sum(self.g_h)
        root_node = Node(id=0,sitename=consts.GUEST,sum_grad=g_sum,sum_hess=h_sum,weight=\
            self.splitter.node_weight(g_sum,h_sum))
        self.cur_layer_node = [root_node]
        LOGGER.debug('assign samples to root node')
        self.inst2node_idx = self.assign_instance_to_root_node(self.binned_data,0)

        for dep in range(self.max_depth):

            if dep + 1 == self.max_depth:

                for node in self.cur_layer_node:
                    node.is_leaf = True
                    self.tree_node.append(node)
                rest_sample_weights = self.get_node_sample_weights(self.inst2node_idx,self.tree_node)
                if self.sample_weights is None:
                    self.sample_weights = rest_sample_weights
                else:
                    self.sample_weights = self.sample_weights.union(rest_sample_weights)

                # stop fitting
                break

            LOGGER.debug('start to fit layer {}'.format(dep))

            table_with_assignment = self.binned_data.join(self.inst2node_idx,
                                                          lambda inst, assignment: (inst, assignment))

            # send current layer node number:
            # self.sync_cur_layer_node_num(len(self.cur_layer_node),suffix=(dep,self.tree_idx))

            split_info = []
            for batch_id,i in enumerate(range(0,len(self.cur_layer_node),self.max_split_nodes)):
                cur_to_split = self.cur_layer_node[i:i+self.max_split_nodes]

                node_map = {node.id:idx for idx,node in enumerate(cur_to_split)}
                LOGGER.debug('computing histogram for batch{} at depth{}'.format(batch_id,dep))
                acc_histogram = self.get_local_histogram(node_map,self.g_h,table_with_assignment,self.bin_split_points,
                                                         self.bin_sparse_points,self.valid_features)


                # test federations
                for bag in acc_histogram:
                    print('before:',bag)
                    # bag.add_inplace(bag)
                    # print('after',bag)

                LOGGER.debug('federated finding best splits for batch{}'.format(batch_id))
                # self.sync_local_node_histogram(acc_histogram, suffix=(batch_id, dep, self.tree_idx))

                # for local testing
                split_info += self.splitter.find_split(acc_histogram,use_missing=self.use_missing,zero_as_missing=
                                                       self.zero_as_missing,valid_features=self.valid_features)


            # split_info = self.sync_best_splits(suffix=(dep,self.tree_idx))
            LOGGER.debug('got best splits from host')

            self.split_history.append(split_info)

            new_layer_node = self.update_tree(self.cur_layer_node,split_info)
            self.cur_layer_node = new_layer_node
            self.update_feature_importance(split_info)

            self.inst2node_idx,leaf_val = self.assign_instance_to_new_node(table_with_assignment, self.tree_node)

            # for testing
            print(list(self.inst2node_idx.collect()))
            self.print_inst2node(self.inst2node_idx)

            # record leaf val
            if self.sample_weights is None:
                self.sample_weights = leaf_val
            else:
                self.sample_weights = self.sample_weights.union(leaf_val)

            LOGGER.debug('assigning instance to new nodes done')

        self.convert_bin_to_val()

        LOGGER.debug('fitting tree done')
        LOGGER.debug('tree node num is {}'.format(len(self.tree_node)))


    def traverse_tree(self,data_inst:Instance,tree:List[Node],use_missing=True,zero_as_missing=True):

        nid = 0 # root node id
        while True:

            if tree[nid].is_leaf:
                return tree[nid].weight

            cur_node = tree[nid]
            fid,bid = cur_node.fid,cur_node.bid
            missing_dir = cur_node.missing_dir

            if use_missing and zero_as_missing:

                if data_inst.features.get_data(fid) == NoneType() or data_inst.features.get_data(fid, None) is None:

                    nid = tree[nid].right_nodeid if missing_dir == 1 else tree[nid].left_nodeid

                elif data_inst.features.get_data(fid) <= bid:
                    nid = tree[nid].left_nodeid
                else:
                    nid = tree[nid].right_nodeid

            elif data_inst.features.get_data(fid) == NoneType():

                nid = tree[nid].right_nodeid if missing_dir == 1 else tree[nid].left_nodeid

            elif data_inst.features.get_data(fid, 0) <= bid:
                nid = tree[nid].left_nodeid
            else:
                nid = tree[nid].right_nodeid


    def predict(self,data_inst:DTable):

        LOGGER.debug('tree start to predict')

        traverse_tree = functools.partial(self.traverse_tree,
                                          tree=self.tree_node,
                                          use_missing=self.use_missing,
                                          zero_as_missing=self.zero_as_missing,)

        predicted_weights = data_inst.mapValues(traverse_tree)

        LOGGER.debug('predicting done')

        return predicted_weights

    def print_split_history(self):
        for idx,l in enumerate(self.split_history):
            print('layer {}'.format(idx))
            for info in l:
                print(info)

    def print_leafs(self):
        for node in self.tree_node:
            print(node)

    def print_inst2node(self,inst2node:DTable):
        assign = list(inst2node.collect())
        data = []
        for i in assign:
            data.append([i[0],i[1][1]])
        print(pd.DataFrame(data))

    def set_model_meta(self):
        pass

    def set_model_param(self):
        pass
