import json
import os

from networkx.readwrite import json_graph
from torch_geometric.datasets import Actor
import torch_geometric.transforms as T

try:
    import dgl
    from dgl.data import CiteseerGraphDataset, CoraGraphDataset, PubmedGraphDataset, CoauthorCSDataset, AmazonCoBuyComputerDataset, AmazonCoBuyPhotoDataset, CoauthorPhysicsDataset, FraudDataset, FlickrDataset, YelpDataset
except:
    print("dgl not imported, install chardet!")
import torch
import numpy as np
from torch_scatter import scatter_add
from torch_geometric.datasets import WebKB, WikipediaNetwork, WikiCS

from Citation import citation_datasets
from preprocess import load_syn


def get_dataset(name, path, split_type='public'):
    import torch_geometric.transforms as T
    from torch_geometric.datasets import Coauthor
    if name == "Cora" or name == "CiteSeer" or name == "PubMed":
        from torch_geometric.datasets import Planetoid
        dataset = Planetoid(path, name, transform=T.NormalizeFeatures(), split=split_type)
    elif name == 'Amazon-Computers':
        from torch_geometric.datasets import Amazon
        return Amazon(root=path, name='computers', transform=T.NormalizeFeatures())
    elif name == 'Amazon-Photo':
        from torch_geometric.datasets import Amazon
        return Amazon(root=path, name='photo', transform=T.NormalizeFeatures())
    elif name == 'Coauthor-CS':
        return Coauthor(root=path, name='cs', transform=T.NormalizeFeatures())
    elif name == 'Coauthor-physics':
        return Coauthor(root=path, name='physics', transform=T.NormalizeFeatures())
    elif name == 'ppi':     # TODO
        dataset_dir = './data/ppi_data'
        G = json_graph.node_link_graph(json.load(open(dataset_dir + "/ppi-G.json")))
        labels = json.load(open(dataset_dir + "/ppi-class_map.json"))
        labels = {int(i): l for i, l in labels.iteritems()}

        train_ids = [n for n in G.nodes() if not G.node[n]['val'] and not G.node[n]['test']]
        test_ids = [n for n in G.nodes() if G.node[n][setting]]
        train_labels = np.array([labels[i] for i in train_ids])
        if train_labels.ndim == 1:
            train_labels = np.expand_dims(train_labels, 1)
        test_labels = np.array([labels[i] for i in test_ids])

        embeds = np.load(data_dir + "/val.npy")
        id_map = {}
        with open(data_dir + "/val.txt") as fp:
            for i, line in enumerate(fp):
                id_map[int(line.strip())] = i
        train_embeds = embeds[[id_map[id] for id in train_ids]]
        test_embeds = embeds[[id_map[id] for id in test_ids]]
    else:
        raise NotImplementedError("Not Implemented Dataset!")

    return dataset

def get_idx_info(label, n_cls, train_mask, device):
    index_list = torch.arange(len(label)).to(device)
    idx_info = []
    for i in range(n_cls):
        cls_indices = index_list[((label == i) & train_mask).to(device)]
        idx_info.append(cls_indices)
    return idx_info

def get_idx_info_multilabel(label, n_cls, train_mask, device):
    index_list = torch.arange(len(label)).to(device)
    idx_info = []
    for i in range(n_cls):
        cls_indices = index_list[((label[i] == 1) & train_mask).to(device)]
        idx_info.append(cls_indices)
    return idx_info


def make_longtailed_data_remove(edge_index, label, n_data, n_cls, ratio, train_mask):
    """
    training split don't influence edge, but make_imbalance will cut edge.
    :param edge_index: all edges in the graph
    :param label: classes of all nodes
    :param n_data:num of train in each class
    :param n_cls:
    :param ratio:
    :param train_mask:
    :return: list(class_num_list), train_mask, idx_info, node_mask, edge_mask
    """
    # Sort from major to minor
    device = edge_index.device
    n_data = torch.tensor(n_data)   # from list to tensor
    sorted_n_data, indices = torch.sort(n_data, descending=True)
    inv_indices = np.zeros(n_cls, dtype=np.int64)
    for i in range(n_cls):
        inv_indices[indices[i].item()] = i
    assert (torch.arange(len(n_data))[indices][torch.tensor(inv_indices)] - torch.arange(len(n_data))).sum().abs() < 1e-12

    # Compute the number of nodes for each class following LT rules
    ratio = torch.tensor(ratio, dtype=torch.float32)   # for mu to convert to numpy
    # Move the tensor to CPU before using it in numpy operations
    if not isinstance(n_cls, int):
        ratio = ratio.cpu()
        n_cls = n_cls.cpu()
    mu = np.power(1/ratio.detach().cpu().numpy(), 1/(n_cls - 1))            # mu is ratio of two classes, while args.ratio is ratio of major to minor
    mu = torch.tensor(mu, dtype=torch.float32, device=ratio.device)

    n_round = []
    class_num_list = []
    for i in range(n_cls):
        # assert int(sorted_n_data[0].item() * np.power(mu, i)) >= 1
        temp = int(sorted_n_data[0].item() * np.power(mu, i))
        if temp< 1:
            temp = 1
        class_num_list.append(int(min(temp, sorted_n_data[i])))
        """
        Note that we remove low degree nodes sequentially (10 steps)
        since degrees of remaining nodes are changed when some nodes are removed
        """
        if i < 1:  # We do not remove any nodes of the most frequent class
            n_round.append(1)
        else:
            n_round.append(10)
    class_num_list = np.array(class_num_list)   # from list to np.array
    class_num_list = class_num_list[inv_indices]    # sorted
    n_round = np.array(n_round)[inv_indices]        # sorted  #

    # Compute the number of nodes which would be removed for each class
    remove_class_num_list = [n_data[i].item()-class_num_list[i] for i in range(n_cls)]
    remove_idx_list = [[] for _ in range(n_cls)]
    # print(remove_idx_list)  # [[], [], [], [], [], [], []]
    cls_idx_list = []   # nodes belong to class i
    index_list = torch.arange(len(train_mask)).to(device)
    original_mask = train_mask.clone()
    for i in range(n_cls):
        cls_idx_list.append(index_list[(label == i) & original_mask])

    for i in indices.numpy():
        for r in range(1, n_round[i]+1):
            # Find removed nodes
            node_mask = label.new_ones(label.size(), dtype=torch.bool).to(device)
            # new_ones is a PyTorch function used to create a new tensor of ones with the specified shape and data type.
            # print("Initialize all true: ", node_mask[:10])
            node_mask[sum(remove_idx_list, [])] = False
            # print("Setting some as false", node_mask[:10])

            # Remove connection with removed nodes
            row, col = edge_index[0].to(device), edge_index[1].to(device)
            # print("row is ", row.shape, row[:10])
            # # torch.Size([10556]) tensor([0, 0, 0, 1, 1, 1, 2, 2, 2, 2])
            # print("col is ", row.shape, col[:10])
            # # torch.Size([10556]) tensor([ 633, 1862, 2582,    2,  652,  654,    1,  332, 1454, 1666])
            row_mask = node_mask[row]
            col_mask = node_mask[col]
            edge_mask = row_mask & col_mask  # elementwise "and"

            # Compute degree
            degree = scatter_add(torch.ones_like(col[edge_mask]), col[edge_mask], dim_size=label.size(0)).to(row.device)
            degree = degree[cls_idx_list[i]]
            _, remove_idx = torch.topk(degree, (r*remove_class_num_list[i])//n_round[i], largest=False)
            remove_idx = cls_idx_list[i][remove_idx]

            # remove_idx_list[i] = list(remove_idx.numpy())
            remove_idx_list[i] = list(remove_idx.cpu().numpy())     # Ben for GPU

    # Find removed nodes
    node_mask = label.new_ones(label.size(), dtype=torch.bool)
    node_mask[sum(remove_idx_list, [])] = False

    # Remove connection with removed nodes
    row, col = edge_index[0], edge_index[1]
    row_mask = node_mask[row]
    col_mask = node_mask[col]
    edge_mask = row_mask & col_mask     #

    train_mask = node_mask & train_mask
    idx_info = []
    for i in range(n_cls):
        cls_indices = index_list[(label == i) & train_mask]
        idx_info.append(cls_indices)

    # row, col = edge_index[0], edge_index[1]
    # row_mask = train_mask[row]
    # col_mask = train_mask[col]
    # train_edge_mask = row_mask & col_mask  #
    # # train_edge_mask = edge_mask & train_mask

    return list(class_num_list), train_mask, idx_info, node_mask, edge_mask

def keep_all_data(edge_index, label, n_data, n_cls, train_mask):
    device = edge_index.device
    class_num_list = n_data
    data_train_mask = train_mask

    index_list = torch.arange(len(train_mask)).to(device)
    idx_info = []
    for i in range(n_cls):
        cls_indices = index_list[(label == i) & train_mask]
        idx_info.append(cls_indices)

    train_node_mask = train_mask.to(device)

    edge_mask = torch.ones(edge_index.size(1), dtype=torch.bool)   # Qin revise May16
    return class_num_list, data_train_mask, idx_info, train_node_mask, edge_mask

def keep_all_data_classBalanced(edge_index, label, n_data, n_cls, train_mask):
    device = edge_index.device
    class_num_list = n_data
    data_train_mask = train_mask

    index_list = torch.arange(len(train_mask)).to(device)
    idx_info = []
    for i in range(n_cls):
        cls_indices = index_list[(label == i) & train_mask]
        idx_info.append(cls_indices)

    train_node_mask = train_mask.to(device)

    edge_mask = torch.ones(edge_index.size(1), dtype=torch.bool)   # Qin revise May16
    return class_num_list, data_train_mask, idx_info, train_node_mask, edge_mask


def load_directedData(args):
    os.chdir(os.path.dirname(os.path.abspath(__file__)))
    load_func, subset = args.Direct_dataset.split('/')[0], args.Direct_dataset.split('/')[1]
    if load_func == 'WebKB':
        load_func = WebKB
        dataset = load_func(root=args.data_path, name=subset)
    elif load_func == 'WikipediaNetwork':
        load_func = WikipediaNetwork
        dataset = load_func(root=args.data_path, name=subset)
    elif load_func == 'WikiCS':
        load_func = WikiCS
        # dataset = load_func(root=args.data_path)        # get undirected
        dataset = load_func(root=args.data_path, is_undirected=False)
    elif load_func == 'cora_ml':
        dataset = citation_datasets(root='data/cora_ml.npz')
    elif load_func == 'citeseer_npz':
        dataset = citation_datasets(root='data/citeseer_npz.npz')
    elif load_func in ['film']:
        dataset = Actor(root='../datasets/film', transform=T.NormalizeFeatures())

    elif load_func == 'dgl':    # Ben
        subset = subset.lower()
        try:
            dataset = load_dgl_directed(subset)
        except NotImplementedError:
            print("Load data unexpected: undirected data!")
            dataset = load_dgl_bidirected(args)
    elif load_func == 'telegram':
        dataset = load_syn(root='data/telegram')
    else:
        # dataset = load_syn(args.data_path + load_func+ '/'+ subset, None)
        dataset = load_syn(load_func+ '/'+ subset, None)

    return dataset

def load_dgl_directed(subset):
    if subset == 'citeseer':    # Nodes: 3327, Edges: 9228, Number of Classes: 6
        return CiteseerGraphDataset(reverse_edge=False)
    elif subset == 'cora':  # Nodes: 2708, Edges: 10556, Number of Classes: 7
        return CoraGraphDataset(reverse_edge=False)
    elif subset == 'pubmed':    # Nodes: 19717, Edges: 88651
        dataset = PubmedGraphDataset(reverse_edge=False)
    elif subset== 'coauthor-cs':   # bidirected
        dataset = CoauthorCSDataset()
    elif subset== 'coauthor-ph':   # bidirected
        dataset = CoauthorPhysicsDataset()
    elif subset == 'computer':
        dataset = AmazonCoBuyComputerDataset()
    elif subset == 'photo':
        dataset = AmazonCoBuyPhotoDataset()
    elif subset == 'reddit':
        from dgl.data import RedditDataset
        dataset = RedditDataset()
    elif subset == 'Fyelp':
        dataset = FraudDataset('yelp')
    elif subset == 'Famazon':
        dataset = FraudDataset('amazon')
    elif subset == 'flickr':
        dataset = FlickrDataset()
    elif subset == 'yelp':
        dataset = YelpDataset()
    # all below not working
    elif subset == 'aifb':  # Nodes: 7262, Edges: 48810 (including reverse edges)
        dataset = dgl.data.rdf.AIFBDataset(insert_reverse=False)    # don't have data_x  #
        #  assortative , node classification
    elif subset =='mutag':  # Nodes: 27163, Edges: 148100 (including reverse edges), 2 class
        dataset = dgl.data.rdf.MUTAGDataset(insert_reverse=False)   # for graph classification
    elif subset == 'bgs':   # Nodes: 94806,  Edges: 672884 (including reverse edges), 2 class
        dataset = dgl.data.rdf.BGSDataset(insert_reverse=False)     # not work to load
    elif subset == 'am':   # Nodes: 881680  Edges: 5668682 (including reverse edges)
        dataset = dgl.data.rdf.AMDataset(insert_reverse=False)
    else:
        raise NotImplementedError
    return dataset

def random_planetoid_splits(data, y, train_ratio=0.7, val_ratio=0.1, percls_trn=20,  val_lb=30, num_splits=10, Flag=1):
    # Set new random planetoid splits based on provided ratios
    num_node = y.size()[0]
    data.train_mask = torch.zeros(num_node, num_splits, dtype=torch.bool)
    data.val_mask = torch.zeros(num_node, num_splits, dtype=torch.bool)
    data.test_mask = torch.zeros(num_node, num_splits, dtype=torch.bool)

    for split_idx in range(num_splits):
        for i in range(y.max().item() + 1):
            index = (y == i).nonzero().view(-1)

            if Flag == 1:
                train_size = percls_trn
                val_size = val_lb
            else:       # If Flag is 0, use ratio split
                total = index.size(0)
                train_size = int(train_ratio * total)
                val_size = int(val_ratio * total)

            train_indices = index[:train_size]
            val_indices = index[train_size:train_size + val_size]
            test_indices = index[train_size + val_size:]

            # Assign masks
            data.train_mask[train_indices, split_idx] = 1
            data.val_mask[val_indices, split_idx] = 1
            data.test_mask[test_indices, split_idx] = 1
        index = index[torch.randperm(index.size(0))]

    return data




def get_step_split(valid_each, labeling_ratio, all_idx, all_label, nclass):
    imb_ratio =1
    base_valid_each = valid_each

    head_list = [i for i in range(nclass//2)] 

    all_class_list = [i for i in range(nclass)]
    tail_list = list(set(all_class_list) - set(head_list))

    h_num = len(head_list)
    t_num = len(tail_list)

    base_train_each = int(len(all_idx) * labeling_ratio / (t_num + h_num * imb_ratio))

    idx2train,idx2valid = {},{}

    total_train_size = 0
    total_valid_size = 0

    for i_h in head_list: 
        idx2train[i_h] = int(base_train_each * imb_ratio)
        idx2valid[i_h] = int(base_valid_each * 1) 

        total_train_size += idx2train[i_h]
        total_valid_size += idx2valid[i_h]

    for i_t in tail_list: 
        idx2train[i_t] = int(base_train_each * 1)
        idx2valid[i_t] = int(base_valid_each * 1)

        total_train_size += idx2train[i_t]
        total_valid_size += idx2valid[i_t]

    train_list = [0 for _ in range(nclass)]
    train_node = [[] for _ in range(nclass)]
    train_idx  = []

    for iter1 in all_idx:
        iter_label = all_label[iter1]
        if train_list[iter_label] < idx2train[iter_label]:
            train_list[iter_label]+=1
            train_node[iter_label].append(iter1)
            train_idx.append(iter1)

        if sum(train_list)==total_train_size:break

    assert sum(train_list)==total_train_size

    after_train_idx = list(set(all_idx)-set(train_idx))

    valid_list = [0 for _ in range(nclass)]
    valid_idx  = []
    for iter2 in after_train_idx:
        iter_label = all_label[iter2]
        if valid_list[iter_label] < idx2valid[iter_label]:
            valid_list[iter_label]+=1
            valid_idx.append(iter2)
        if sum(valid_list)==total_valid_size:break

    test_idx = list(set(after_train_idx)-set(valid_idx))

    return train_idx, valid_idx, test_idx, train_node

