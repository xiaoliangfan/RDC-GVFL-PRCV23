import torch
from model import GCN, SGC
from utils import utils
from flow import logger
import importlib
import scipy.sparse as sp
from utils.utils import sparse_mx_to_torch_sparse_tensor


class Client(object):
    def __init__(self, cid, para_dict, data_dict):
        # 用户索引
        self.cid = cid
        self.device = para_dict['device']

        # 用户拥有的数据
        self.features = data_dict['features_list'][cid]     # 每个client拥有的特征                  ndarray
        self.adj = data_dict['adj']                         # 每个client拥有的邻接矩阵               csr_matrix
        self.adj_norm = None                                # 在preprocess中进行处理
        # self.adj_norm = normalize_adj(self.adj)           # A'=(D+I)^-1/2 * (A+I) * (D+I)^-1/2  lil_matrix
        # self.labels = data_dict['labels']                 # 每个client没有标签
        self.train_idx = data_dict['train_idx']             # ndarray
        self.val_idx = data_dict['val_idx']                 # ndarray
        self.test_idx = data_dict['test_idx']               # ndarray
        # 将变量转为tensor并归一化adj
        self.preprocess()                                   # adj: sparse tensor(gpu); other var: tensor(gpu)

        # 训练相关的参数
        lr = para_dict['lr']
        weight_decay = para_dict['weight_decay']
        in_dim = self.features.shape[1]
        hid_dim = para_dict['hid_dim']
        out_dim = para_dict['out_dim']

        # 不同的local model client和server做出的响应会不一样;RGCN的话,每个用户上传的都是高斯向量,如何concat呢？:不考虑RGCN了
        if para_dict['model'] == 'GCN':
            self.local_model = GCN(nfeat=in_dim, nhid=hid_dim, nemb=out_dim, dropout=0, device=self.device)
        elif para_dict['model'] == 'SGC':
            self.local_model = SGC(nfeat=in_dim, nemb=out_dim, device=self.device)
        self.optimizer = torch.optim.Adam(self.local_model.parameters(), lr=lr, weight_decay=weight_decay)

    def preprocess(self):
        """
        初始化阶段，client会先执行preprocess，因此client初始化完了，这些变量都会转成tensor，并挪到cpu/gpu上
        将变量转成tensor/sparse tensor, 移到gpu, 归一化adj;次序也可以颠倒,但相应的实现也要调整
        """
        if type(self.adj) is not torch.Tensor:
            self.adj, self.features = utils.to_tensor(self.adj, self.features, device=self.device)
        else:
            self.features = self.features.to(self.device)
            self.adj = self.adj.to(self.device)

        if utils.is_sparse_tensor(self.adj):
            self.adj_norm = utils.normalize_adj_tensor(self.adj, sparse=True)
        else:
            self.adj_norm = utils.normalize_adj_tensor(self.adj)

    def output(self, is_train: bool = False, target_node=None):
        """
        无target_node->输出全部节点的emb; 有target_node->目标节点的emb
        :param is_train:    为malicious预留的接口
        :param target_node: 目标节点(也是为malicious预留的接口)
        :return:            返回全部节点的emb或目标节点的emb
        """
        embedding = self.local_model(self.features, self.adj_norm)
        if target_node is None:
            return embedding                            # emb: (2708, 16)
        else:
            return embedding[[target_node]]             # emb[[target_node]]==emb[[target_node], :]: (1, 16)


class Malicious(Client):
    def __init__(self, cid, para_dict, data_dict):
        super(Malicious, self).__init__(cid, para_dict, data_dict)
        self.para_dict = para_dict
        self.data_dict = data_dict

        self.attack_method = para_dict['attack']
        self.adj_ptb = None
        self.adj_ptb_norm = None

    def preprocess_ptb(self):
        """
        将adj_ptb转成sparse tensor, 移到gpu, 归一化adj_ptb
        """
        if type(self.adj_ptb) is not torch.Tensor:
            if sp.issparse(self.adj_ptb):
                self.adj_ptb = sparse_mx_to_torch_sparse_tensor(self.adj_ptb).to(self.device)
            else:
                self.adj_ptb = torch.FloatTensor(self.adj_ptb).to(self.device)
        else:
            self.adj_ptb = self.adj_ptb.to(self.device)

        if utils.is_sparse_tensor(self.adj_ptb):
            self.adj_ptb_norm = utils.normalize_adj_tensor(self.adj_ptb, sparse=True)
        else:
            self.adj_ptb_norm = utils.normalize_adj_tensor(self.adj_ptb)

    def output(self, is_train: bool = False, target_node=None):
        """
        无target_node->输出全部节点的emb; 有target_node->目标节点的emb
        :param is_train:
        :param target_node:
        :return: 返回全部节点的emb或目标节点的emb
        """
        if is_train:
            # output
            embedding = self.local_model(self.features, self.adj_norm)
            return embedding
        else:
            # attack
            if target_node is None:
                raise "attacked node is lacked!"
            if self.attack_method in ['GF', 'GF_pgd', 'Nettack']:
                self.adj_ptb = self.attack(self.attack_method, self.para_dict, self.data_dict, target_node)
                self.preprocess_ptb()
                embedding = self.local_model(self.features, self.adj_ptb_norm)
            elif self.attack_method in ['Gaussian', 'Missing', 'Flipping']:
                embedding = self.attack(self.attack_method, self.para_dict, self.data_dict, target_node)
            else:
                raise "no attack method!"
            # output
            if target_node is None:
                return embedding                    # emb: (2708, 16)
            else:
                return embedding[[target_node]]     # emb[[target_node]]==emb[[target_node], :]: (1, 16)

    def attack(self, attack_method, para_dict, data_dict, target_node):
        """
        攻击
        :param attack_method:
        :param para_dict:
        :param data_dict:
        :param target_node:
        :return: modified_adj   type:csr
        """
        special = True if attack_method in ['GF', 'GF_pgd', 'Nettack'] else False       # 是否是定制化攻击
        attack = getattr(importlib.import_module('.'.join(['attack', f'{attack_method}'])), 'attack')
        if special:
            if attack_method in ['GF', 'GF_pgd'] and not hasattr(self, 'shadow_global_model'):
                infer_global_model = getattr(importlib.import_module
                                             ('.'.join(['attack', f'{attack_method}'])), 'infer_global_model')
                self.shadow_global_model = infer_global_model(self, para_dict, data_dict)

            if attack_method == 'Nettack' and not hasattr(self, 'surrogate_model'):
                train_surrogate_model = getattr(importlib.import_module
                                                ('.'.join(['attack', f'{attack_method}'])), 'train_surrogate_model')
                self.surrogate_model = train_surrogate_model(self, para_dict, data_dict)

            modified_adj = attack(self, para_dict, data_dict, target_node)
            return modified_adj
        else:
            embedding = attack(self, para_dict, data_dict, target_node)
            return embedding
