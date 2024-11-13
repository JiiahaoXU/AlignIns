import copy

import torch
from torch.nn.utils import parameters_to_vector
import numpy as np
import logging
from utils import vector_to_model, vector_to_name_param

import sklearn.metrics.pairwise as smp
from geom_median.torch import compute_geometric_median 


class Aggregation():
    def __init__(self, agent_data_sizes, n_params, poisoned_val_loader, args, writer):
        self.agent_data_sizes = agent_data_sizes
        self.args = args
        self.writer = writer
        self.server_lr = args.server_lr
        self.n_params = n_params
        self.poisoned_val_loader = poisoned_val_loader
        self.cum_net_mov = 0
        self.update_last_round = None
        self.memory_dict = dict()
        self.wv_history = []
        
         
    def aggregate_updates(self, global_model, agent_updates_dict):

        if self.args.attack == 'lie':
            all_updates = []
            for id, update in agent_updates_dict.items():
                all_updates.append(agent_updates_dict[id])

            est_updates = torch.stack(all_updates)

            mu = torch.mean(est_updates, dim=0)
            sigma = torch.std(est_updates, dim=0)
            z = 1.5  # 0.3, 1.5 #Pre-calculated value for z_{max} from z-table, based on n=50, m=24 (and hence, s=2)
            minn = mu - z * sigma
            maxx = mu + z * sigma
            for id, update in agent_updates_dict.items():
                if id < self.args.num_corrupt:
                    # agent_updates_dict[id] *= total_num_dps_per_round / num_dps_poisoned_dataset
                    agent_updates_dict[id] = torch.where(agent_updates_dict[id] < minn, minn, agent_updates_dict[id])
                    agent_updates_dict[id] = torch.where(agent_updates_dict[id] > maxx, maxx, agent_updates_dict[id])

        if self.args.attack == 'lie_byz':
            all_updates = []
            for id, update in agent_updates_dict.items():
                all_updates.append(agent_updates_dict[id])

            est_updates = torch.stack(all_updates)

            mu = torch.mean(est_updates, dim=0)
            sigma = torch.std(est_updates, dim=0)
            z = 1.5  # 0.3, 1.5 #Pre-calculated value for z_{max} from z-table, based on n=50, m=24 (and hence, s=2)
            byz_grad = mu - z * sigma

            m1 = int(0.5 * self.args.num_corrupt)
            m2 = self.args.num_corrupt - m1
            # byz_grads1 = [byz_grad] * m1
            byz_grad2 = ((self.args.num_agents - self.args.num_corrupt-m1)*byz_grad-torch.sum(est_updates, dim=0))/m2
            # byz_grads2 = [byz_grad2] * m2
            for id, update in agent_updates_dict.items():
                if id < self.args.num_corrupt and id <m1:
                    # agent_updates_dict[id] *= total_num_dps_per_round / num_dps_poisoned_dataset
                    agent_updates_dict[id] = byz_grad
                elif id < self.args.num_corrupt and id <m1+m2:
                    agent_updates_dict[id] = byz_grad2

        lr_vector = torch.Tensor([self.server_lr]*self.n_params).to(self.args.device)
        if self.args.aggr != "rlr":
            lr_vector = lr_vector
        else:
            lr_vector, _ = self.compute_robustLR(agent_updates_dict)
        # mask = torch.ones_like(agent_updates_dict[0])
        aggregated_updates = 0
        cur_global_params = parameters_to_vector(
            [global_model.state_dict()[name] for name in global_model.state_dict()]).detach()
        if self.args.aggr=='avg' or self.args.aggr == 'rlr' or self.args.aggr == 'lockdown':          
            aggregated_updates = self.agg_avg(agent_updates_dict)

        if self.args.aggr == 'alignins':
            aggregated_updates = self.agg_alignins(agent_updates_dict, cur_global_params)
            torch.cuda.empty_cache()
        if self.args.aggr == 'deepsight':
            aggregated_updates = self.agg_deepsight(agent_updates_dict, global_model, cur_global_params)
        if self.args.aggr == 'mmetric':
            aggregated_updates = self.agg_mul_metric(agent_updates_dict, global_model, cur_global_params)
        if self.args.aggr == 'foolsgold':
            aggregated_updates = self.agg_foolsgold(agent_updates_dict)
        if self.args.aggr == 'signguard':
            aggregated_updates = self.agg_signguard(agent_updates_dict)

        if self.args.aggr=='avg_pruning':          
            aggregated_updates = self.agg_avg_pruning(agent_updates_dict)
        if self.args.aggr== "clip_avg":
            for _id, update in agent_updates_dict.items():
                weight_diff_norm = torch.norm(update).item()
                logging.info(weight_diff_norm)
                update.data = update.data / max(1, weight_diff_norm / 2)
            aggregated_updates = self.agg_avg(agent_updates_dict)
            logging.info(torch.norm(aggregated_updates))
        elif self.args.aggr=='comed':
            aggregated_updates = self.agg_comed(agent_updates_dict)
        elif self.args.aggr == 'sign':
            aggregated_updates = self.agg_sign(agent_updates_dict)
        elif self.args.aggr == "krum":
            aggregated_updates = self.agg_krum(agent_updates_dict)
        elif self.args.aggr == "mkrum" or self.args.aggr == 'lead':
            aggregated_updates = self.agg_mkrum(agent_updates_dict)
        elif self.args.aggr == "rfa":
            aggregated_updates = self.agg_gm(agent_updates_dict)
        elif self.args.aggr == "tm":
            aggregated_updates = self.agg_tm(agent_updates_dict)
        neurotoxin_mask = {}
        updates_dict = vector_to_name_param(aggregated_updates, copy.deepcopy(global_model.state_dict()))
        for name in updates_dict:
            updates = updates_dict[name].abs().view(-1)
            gradients_length = torch.numel(updates)
            _, indices = torch.topk(-1 * updates, int(gradients_length * self.args.dense_ratio))
            mask_flat = torch.zeros(gradients_length)
            mask_flat[indices.cpu()] = 1
            neurotoxin_mask[name] = (mask_flat.reshape(updates_dict[name].size()))

        cur_global_params = parameters_to_vector([ global_model.state_dict()[name] for name in global_model.state_dict()]).detach()
        new_global_params =  (cur_global_params + lr_vector*aggregated_updates).float()
        vector_to_model(new_global_params, global_model)
        return updates_dict, neurotoxin_mask

    def agg_rfa(self, agent_updates_dict):
        local_updates = []
        benign_id = []
        malicious_id = []

        for _id, update in agent_updates_dict.items():
            local_updates.append(update)
            if _id < self.args.num_corrupt:
                malicious_id.append(_id)
            else:
                benign_id.append(_id)

        chosen_clients = malicious_id + benign_id
        num_chosen_clients = len(malicious_id + benign_id)
        n = len(local_updates)
        grads = torch.stack(local_updates, dim=0)
        weights = torch.ones(n).to(self.args.device)  
        gw = compute_geometric_median(local_updates, weights).median
        for i in range(2):
            weights = torch.mul(weights, torch.exp(-1.0*torch.norm(grads-gw, dim=1)))
            gw = compute_geometric_median(local_updates, weights).median

        aggregated_model = gw
        return aggregated_model

    def agg_alignins(self, agent_updates_dict, flat_global_model):
        local_updates = []
        benign_id = []
        malicious_id = []

        for _id, update in agent_updates_dict.items():
            local_updates.append(update)
            if _id < self.args.num_corrupt:
                malicious_id.append(_id)
            else:
                benign_id.append(_id)

        chosen_clients = malicious_id + benign_id
        num_chosen_clients = len(malicious_id + benign_id)
        temp_grads = torch.stack(local_updates, dim=0)

        update_cos = []
        update_pdp = []
        major_sign = torch.sign(torch.sum(torch.sign(temp_grads), dim=0))
        cos = torch.nn.CosineSimilarity(dim=0, eps=1e-6)
        for i in range(len(temp_grads)):
            _, init_indices = torch.topk(torch.abs(temp_grads[i]), int(len(temp_grads[i]) * self.args.sparsity))

            update_pdp.append((torch.sum(torch.sign(temp_grads[i][init_indices]) == major_sign[init_indices]) / torch.numel(temp_grads[i][init_indices])).item())
    
            update_cos.append(cos(temp_grads[i], flat_global_model).item())


        logging.info('COS: %s' % [round(i, 4) for i in update_cos])
        logging.info('PDP: %s' % [round(i, 4) for i in update_pdp])

        pdp_std = np.std(update_pdp)
        pdp_med = np.median(update_pdp)

        one_pdp = []
        for i in range(len(update_pdp)):
            one_pdp.append(np.abs(update_pdp[i] - pdp_med) / pdp_std)

        logging.info('stand pdp: %s' % [round(i, 4) for i in one_pdp])
        
        cos_std = np.std(update_cos)
        cos_med = np.median(update_cos)
        one_cos = []
        for i in range(len(update_cos)):
            one_cos.append(np.abs(update_cos[i] - cos_med) / cos_std)

        logging.info('stand cos: %s' % [round(i, 4) for i in one_cos])


        benign_idx1 = set([i for i in range(num_chosen_clients)])
        benign_idx1 = benign_idx1.intersection(set([int(i) for i in np.argwhere(np.array(one_pdp) < self.args.lambda_s)]))
        benign_idx2 = set([i for i in range(num_chosen_clients)])
        benign_idx2 = benign_idx2.intersection(set([int(i) for i in np.argwhere(np.array(one_cos) < self.args.lambda_c)]))

        benign_set = benign_idx2.intersection(benign_idx1)
        
        benign_idx = list(benign_set)
        if len(benign_idx) == 0:
            self.update_last_round = torch.zeros_like(local_updates[0])
            return self.update_last_round

        grads = torch.stack([local_updates[i] for i in benign_idx], dim=0)

        grad_norm = torch.norm(grads, dim=1).reshape((-1, 1))
        norm_clip = grad_norm.median(dim=0)[0].item()
        grads = torch.stack(local_updates, dim=0)
        grad_norm = torch.norm(grads, dim=1).reshape((-1, 1))
        grad_norm_clipped = torch.clamp(grad_norm, 0, norm_clip, out=None)
        # del grad_norm
        
        grads = (grads/grad_norm)*grad_norm_clipped

        correct = 0
        for idx in benign_idx:
            if idx >= len(malicious_id):
                correct += 1

        TPR = correct / len(benign_id)

        if len(malicious_id) == 0:
            FPR = 0
        else:
            wrong = 0
            for idx in benign_idx:
                if idx < len(malicious_id):
                    wrong += 1
            FPR = wrong / len(malicious_id)

        logging.info('benign update index:   %s' % str(benign_id))
        logging.info('selected update index: %s' % str(benign_idx))

        logging.info('FPR:       %.4f'  % FPR)
        logging.info('TPR:       %.4f' % TPR)

        current_dict = {}
        for idx in benign_idx:
            current_dict[chosen_clients[idx]] = grads[idx]

        self.update_last_round = self.agg_avg(current_dict)
        return self.update_last_round

    def agg_avg(self, agent_updates_dict):
        """ classic fed avg """

        sm_updates, total_data = 0, 0
        for _id, update in agent_updates_dict.items():
            n_agent_data = self.agent_data_sizes[_id]
            sm_updates +=  n_agent_data * update
            total_data += n_agent_data
        return  sm_updates / total_data

    def agg_avg_norm_clip(self, agent_updates_dict):
        """ classic fed avg """

        sm_updates, total_data = 0, 0

        grads = []
        for _id, update in agent_updates_dict.items():
            grads.append(update)

        grads = torch.stack(grads, dim=0)

        grad_norm = torch.norm(grads, dim=1).reshape((-1, 1))
        norm_clip = grad_norm.median(dim=0)[0].item()
        grad_norm_clipped = torch.clamp(grad_norm, 0, norm_clip, out=None)
        grads_clip = (grads/grad_norm)*grad_norm_clipped

        for (_id, update), clipped_update in zip(agent_updates_dict.items(), grads_clip):
            n_agent_data = self.agent_data_sizes[_id]
            sm_updates +=  n_agent_data * clipped_update
            total_data += n_agent_data
        return  sm_updates / total_data

    def agg_avg_pruning(self, agent_updates_dict):
        """ classic fed avg """

        sm_updates, total_data = 0, 0
        for _id, update in agent_updates_dict.items():
            n_agent_data = self.agent_data_sizes[_id]

            pruning_mask = self.generate_mask(update, self.args.sparsity)

            update *= pruning_mask
            print('local updates pruned!')

            sm_updates +=  n_agent_data * update
            total_data += n_agent_data
        return  sm_updates / total_data

    def generate_mask(self, vector, sparsity):

        _, indices = torch.topk(torch.abs(vector), int(len(vector) * (1 - sparsity)))
        mask = torch.zeros(len(vector)).cuda()
        mask[indices] = 1.0

        return mask

    
    def agg_comed(self, agent_updates_dict):
        agent_updates_col_vector = [update.view(-1, 1) for update in agent_updates_dict.values()]
        concat_col_vectors = torch.cat(agent_updates_col_vector, dim=1)
        return torch.median(concat_col_vectors, dim=1).values
    
    def agg_sign(self, agent_updates_dict):
        """ aggregated majority sign update """
        agent_updates_sign = [torch.sign(update) for update in agent_updates_dict.values()]
        sm_signs = torch.sign(sum(agent_updates_sign))
        return torch.sign(sm_signs)

    def agg_krum(self, agent_updates_dict):
        krum_param_m = 1
        def _compute_krum_score( vec_grad_list, byzantine_client_num):
            krum_scores = []
            num_client = len(vec_grad_list)
            for i in range(0, num_client):
                dists = []
                for j in range(0, num_client):
                    if i != j:
                        dists.append(
                            torch.norm(vec_grad_list[i]- vec_grad_list[j])
                            .item() ** 2
                        )
                dists.sort()  # ascending
                score = dists[0: num_client - byzantine_client_num - 2]
                krum_scores.append(sum(score))
            return krum_scores
        

        # Compute list of scores
        __nbworkers = len(agent_updates_dict)
        krum_scores = _compute_krum_score(agent_updates_dict, self.args.num_corrupt)
        score_index = torch.argsort(
            torch.Tensor(krum_scores)
        ).tolist()  # indices; ascending
        score_index = score_index[0: krum_param_m]
        return_gradient = [agent_updates_dict[i] for i in score_index]




        return sum(return_gradient)/len(return_gradient)
    
    def agg_mkrum(self, agent_updates_dict):
        krum_param_m = 10
        def _compute_krum_score( vec_grad_list, byzantine_client_num):
            krum_scores = []
            num_client = len(vec_grad_list)
            for i in range(0, num_client):
                dists = []
                for j in range(0, num_client):
                    if i != j:
                        dists.append(
                            torch.norm(vec_grad_list[i]- vec_grad_list[j])
                            .item() ** 2
                        )
                dists.sort()  # ascending
                score = dists[0: num_client - byzantine_client_num - 2]
                krum_scores.append(sum(score))
            return krum_scores

        benign_id = []
        malicious_id = []

        for _id, update in agent_updates_dict.items():
            # local_updates.append(update)
            if _id < self.args.num_corrupt:
                malicious_id.append(_id)
            else:
                benign_id.append(_id)

        # Compute list of scores
        __nbworkers = len(agent_updates_dict)
        krum_scores = _compute_krum_score(agent_updates_dict, self.args.num_corrupt)
        score_index = torch.argsort(
            torch.Tensor(krum_scores)
        ).tolist()  # indices; ascending
        score_index = score_index[0: krum_param_m]

        print('%d clients are selected' % len(score_index))
        return_gradient = [agent_updates_dict[i] for i in score_index]

        correct = 0
        for idx in score_index:
            if idx >= len(malicious_id):
                correct += 1

        CSR = correct / len(benign_id)

        if len(malicious_id) == 0:
            WSR = 0
        else:
            wrong = 0
            for idx in score_index:
                if idx < len(malicious_id):
                    wrong += 1
            WSR = wrong / len(malicious_id)

        # logging.info('benign update index:   %s' % str(benign_id))
        # logging.info('selected update index: %s' % str(benign_idx))

        logging.info('WSR:       %.4f'  % WSR)
        logging.info('CSR:       %.4f' % CSR)

        return sum(return_gradient)/len(return_gradient)

    def compute_robustLR(self, agent_updates_dict):

        agent_updates_sign = [torch.sign(update) for update in agent_updates_dict.values()]  
        sm_of_signs = torch.abs(sum(agent_updates_sign))
        mask=torch.zeros_like(sm_of_signs)
        mask[sm_of_signs < self.args.theta] = 0
        mask[sm_of_signs >= self.args.theta] = 1
        sm_of_signs[sm_of_signs < self.args.theta] = -self.server_lr
        sm_of_signs[sm_of_signs >= self.args.theta] = self.server_lr
        return sm_of_signs.to(self.args.device), mask

    def agg_mul_metric(self, agent_updates_dict, global_model, flat_global_model):
        local_updates = []
        benign_id = []
        malicious_id = []

        for _id, update in agent_updates_dict.items():
            local_updates.append(update)
            if _id < self.args.num_corrupt:
                malicious_id.append(_id)
            else:
                benign_id.append(_id)

        chosen_clients = malicious_id + benign_id
        num_chosen_clients = len(malicious_id + benign_id)

        vectorize_nets = [update.detach().cpu().numpy() for update in agent_updates_dict.values()]

        cos_dis = [0.0] * len(vectorize_nets)
        length_dis = [0.0] * len(vectorize_nets)
        manhattan_dis = [0.0] * len(vectorize_nets)
        for i, g_i in enumerate(vectorize_nets):
            for j in range(len(vectorize_nets)):
                if i != j:
                    g_j = vectorize_nets[j]

                    cosine_distance = float(
                        (1 - np.dot(g_i, g_j) / (np.linalg.norm(g_i) * np.linalg.norm(g_j))) ** 2)   #Compute the different value of cosine distance
                    manhattan_distance = float(np.linalg.norm(g_i - g_j, ord=1))    #Compute the different value of Manhattan distance
                    length_distance = np.abs(float(np.linalg.norm(g_i) - np.linalg.norm(g_j)))    #Compute the different value of Euclidean distance

                    cos_dis[i] += cosine_distance
                    length_dis[i] += length_distance
                    manhattan_dis[i] += manhattan_distance

        tri_distance = np.vstack([cos_dis, manhattan_dis, length_dis]).T

        cov_matrix = np.cov(tri_distance.T)
        inv_matrix = np.linalg.inv(cov_matrix)

        ma_distances = []
        for i, g_i in enumerate(vectorize_nets):
            t = tri_distance[i]
            ma_dis = np.dot(np.dot(t, inv_matrix), t.T)
            ma_distances.append(ma_dis)

        scores = ma_distances
        print(scores)

        p = 0.3
        p_num = p*len(scores)
        topk_ind = np.argpartition(scores, int(p_num))[:int(p_num)]   #sort

        print(topk_ind)
        current_dict = {}

        for idx in topk_ind:
            current_dict[chosen_clients[idx]] = agent_updates_dict[chosen_clients[idx]]

        # return self.agg_avg_norm_clip(current_dict)
        update = self.agg_avg(current_dict)

        return update
   
    def agg_foolsgold(self, agent_updates_dict):
        def foolsgold(grads):
            """
            :param grads:
            :return: compute similatiry and return weightings
            """
            n_clients = grads.shape[0]
            cs = smp.cosine_similarity(grads) - np.eye(n_clients)

            maxcs = np.max(cs, axis=1)
            # pardoning
            for i in range(n_clients):
                for j in range(n_clients):
                    if i == j:
                        continue
                    if maxcs[i] < maxcs[j]:
                        cs[i][j] = cs[i][j] * maxcs[i] / maxcs[j]
            wv = 1 - (np.max(cs, axis=1))

            wv[wv > 1] = 1
            wv[wv < 0] = 0

            alpha = np.max(cs, axis=1)

            # Rescale so that max value is wv
            wv = wv / np.max(wv)
            wv[(wv == 1)] = .99

            # Logit function
            wv = (np.log(wv / (1 - wv)) + 0.5)
            wv[(np.isinf(wv) + wv > 1)] = 1
            wv[(wv < 0)] = 0

            # wv is the weight
            return wv, alpha

        local_updates = []
        benign_id = []
        malicious_id = []

        for _id, update in agent_updates_dict.items():
            local_updates.append(update)
            if _id < self.args.num_corrupt:
                malicious_id.append(_id)
            else:
                benign_id.append(_id)

        names = malicious_id + benign_id
        num_chosen_clients = len(malicious_id + benign_id)

        client_grads = [update.detach().cpu().numpy() for update in agent_updates_dict.values()]
        grad_len = np.array(client_grads[0].shape).prod()
        # print("client_grads size", client_models[0].parameters())
        # grad_len = len(client_grads)
        # if self.memory is None:
        #     self.memory = np.zeros((self.num_clients, grad_len))
        if len(names) < len(client_grads):
            names = np.append([-1], names)  # put in adv

        num_clients = num_chosen_clients
        memory = np.zeros((num_clients, grad_len))
        grads = np.zeros((num_clients, grad_len))

        for i in range(len(client_grads)):
            # grads[i] = np.reshape(client_grads[i][-2].cpu().data.numpy(), (grad_len))
            grads[i] = np.reshape(client_grads[i], (grad_len))
            if names[i] in self.memory_dict.keys():
                self.memory_dict[names[i]] += grads[i]
            else:
                self.memory_dict[names[i]] = copy.deepcopy(grads[i])
            memory[i] = self.memory_dict[names[i]]
        # self.memory += grads
        use_memory = False

        if use_memory:
            wv, alpha = foolsgold(None)  # Use FG
        else:
            wv, alpha = foolsgold(grads)  # Use FG
        # logger.info(f'[foolsgold agg] wv: {wv}')
        self.wv_history.append(wv)

        # print(self.wv_history)

        # agg_grads = []
        # # Iterate through each layer
        # for i in range(len(client_grads[0])):
        #     assert len(wv) == len(client_grads), 'len of wv {} is not consistent with len of client_grads {}'.format(
        #         len(wv), len(client_grads))
        #     temp = wv[0] * client_grads[0][i]
        #     # print(temp)
        #     # Aggregate gradients for a layer
        #     for c, client_grad in enumerate(client_grads):
        #         if c == 0:
        #             continue
        #         temp += wv[c] * client_grad[i]
        #     temp = temp / len(client_grads)
        #     print(temp)
        #     agg_grads.append(temp)

        print(len(client_grads), len(wv))

        # for i in range(len(client_grads)):
        #     client_grads[i] *= wv[i] 
        
        weighted_updates = [update * wv[i] for update, i in zip(agent_updates_dict.values(), range(len(wv)))]

        aggregated_model = torch.mean(torch.stack(weighted_updates, dim=0), dim=0)

        print(aggregated_model.shape)

        return aggregated_model