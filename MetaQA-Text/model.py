import os
import torch
import torch.nn as nn
import pickle
import math

from utils.BiGRU import GRU, BiGRU

class TransferNet(nn.Module):
    def __init__(self, args, dim_word, dim_hidden, vocab, max_active):
        super().__init__()
        self.args = args
        self.vocab = vocab
        self.max_active = max_active
        
        with open(os.path.join(args.input_dir, 'wiki.pt'), 'rb') as f:
            self.kb_pair = torch.LongTensor(pickle.load(f))
            self.kb_range = torch.LongTensor(pickle.load(f))
            self.kb_desc = torch.LongTensor(pickle.load(f))

        num_words = len(vocab['word2id'])
        num_entities = len(vocab['entity2id'])
        self.num_steps = args.num_steps

        self.desc_encoder = BiGRU(dim_word, dim_hidden, num_layers=1, dropout=0.2)
        self.question_encoder = BiGRU(dim_word, dim_hidden, num_layers=1, dropout=0.2)
        
        self.word_embeddings = nn.Embedding(num_words, dim_word)
        self.word_dropout = nn.Dropout(0.3)
        self.step_encoders = []
        for i in range(self.num_steps):
            m = nn.Sequential(
                nn.Linear(dim_hidden, dim_hidden),
                nn.Tanh()
            )
            self.step_encoders.append(m)
            self.add_module('step_encoders_{}'.format(i), m)
        self.rel_classifier = nn.Linear(dim_hidden, 1)

        self.q_classifier = nn.Linear(dim_hidden, num_entities)

        for m in self.modules():
            if isinstance(m, nn.Linear) or isinstance(m, nn.Conv2d):
                nn.init.kaiming_normal_(m.weight)
                if m.bias is not None:
                    m.bias.data.zero_()

    def follow(self, e, pair, p):
        """
        Args:
            e [num_ent]: entity scores
            pair [rsz, 2]: pairs that are taken into consider
            p [rsz]: transfer probabilities of each pair
        """
        sub, obj = pair[:, 0], pair[:, 1]
        obj_p = e[sub] * p
        out = torch.index_add(torch.zeros_like(e), 0, obj, obj_p)
        return out
        

    def forward(self, questions, e_s, answers = None):
        question_lens = questions.size(1) - questions.eq(0).long().sum(dim=1) # 0 means <PAD>
        q_word_emb = self.word_dropout(self.word_embeddings(questions)) # [bsz, max_q, dim_hidden]
        q_word_h, q_embeddings, q_hn = self.question_encoder(q_word_emb, question_lens) # [bsz, max_q, dim_h], [bsz, dim_h], [num_layers, bsz, dim_h]


        device = q_word_h.device
        bsz, dim_h = q_embeddings.size()
        last_e = e_s
        word_attns = []
        ent_probs = [e_s]
        for t in range(self.num_steps):
            cq_t = self.step_encoders[t](q_embeddings) # [bsz, dim_h]
            q_logits = torch.sum(cq_t.unsqueeze(1) * q_word_h, dim=2) # [bsz, max_q]
            q_dist = torch.softmax(q_logits, 1).unsqueeze(1) # [bsz, 1, max_q]
            word_attns.append(q_dist.squeeze(1))
            ctx_h = (q_dist @ q_word_h).squeeze(1) # [bsz, dim_h]


            e_stack = []
            for i in range(bsz):
                e_idx = [torch.argmax(last_e[i], dim=0).item()] + \
                        last_e[i].gt(0.9).nonzero().squeeze(1).tolist()
                e_idx = e_idx[:min(len(e_idx), self.max_active)] # limit the number of active entities
                rg = []
                for j in set(e_idx):
                    rg.append(torch.arange(self.kb_range[j,0], self.kb_range[j,1]).long().to(device))
                rg = torch.cat(rg, dim=0) # [rsz,]
                rg = rg[:min(len(rg), 5 * self.max_active)] # limit the number of next-hop

                pair = self.kb_pair[rg] # [rsz, 2]
                desc = self.kb_desc[rg] # [rsz, max_desc]
                desc_lens = desc.size(1) - desc.eq(0).long().sum(dim=1)
                desc_word_emb = self.word_dropout(self.word_embeddings(desc))
                desc_word_h, desc_embeddings, _ = self.desc_encoder(desc_word_emb, desc_lens) # [rsz, dim_h]
                d_logit = self.rel_classifier(ctx_h[i:i+1] * desc_embeddings).squeeze(1) # [rsz,]
                d_prob = torch.sigmoid(d_logit) # [rsz,]
                # transfer probability
                e_stack.append(self.follow(last_e[i], pair, d_prob))

            last_e = torch.stack(e_stack, dim=0)


            # reshape >1 scores to 1 in a differentiable way
            m = last_e.gt(1).float()
            z = (m * last_e + (1-m)).detach()
            last_e = last_e / z

            # Specifically for MetaQA: reshape cycle entities to 0, because A-r->B-r_inv->A is not allowed
            # if t > 0:
            #     prev_rel = torch.argmax(rel_probs[-2], dim=1)
            #     curr_rel = torch.argmax(rel_probs[-1], dim=1)
            #     prev_prev_ent_prob = ent_probs[-2]
            #     # in our vocabulary, indices of inverse relations are adjacent. e.g., director:0, director_inv:1
            #     m = torch.zeros((bsz,1)).to(device)
            #     m[(torch.abs(prev_rel-curr_rel)==1) & (torch.remainder(torch.min(prev_rel,curr_rel),2)==0)] = 1
            #     ent_m = m.float() * prev_prev_ent_prob.gt(0.9).float()
            #     last_e = (1-ent_m) * last_e

            # Specifically for MetaQA: for 2-hop questions, topic entity is excluded from answer
            # if t == self.num_steps-1:
            #     stack_rel_probs = torch.stack(rel_probs, dim=1) # [bsz, num_step, num_rel]
            #     stack_rel = torch.argmax(stack_rel_probs, dim=2) # [bsz, num_step]
            #     num_self = stack_rel.eq(self.vocab['relation2id']['<SELF_REL>']).long().sum(dim=1) # [bsz,]
            #     m = num_self.eq(1).float().unsqueeze(1) * e_s
            #     last_e = (1-m) * last_e

            ent_probs.append(last_e.detach())

        # question mask
        q_mask = torch.sigmoid(self.q_classifier(q_embeddings))
        last_e = last_e * q_mask

        if answers is None:
            return {
                'e_score': last_e,
                'word_attns': word_attns,
                'ent_probs': ent_probs
            }

        # Distance loss
        weight = answers * 9 + 1
        loss_score = torch.mean(weight * torch.pow(last_e - answers, 2))

        return {'loss_score': loss_score}