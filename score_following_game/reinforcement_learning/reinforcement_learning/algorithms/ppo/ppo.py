import numpy as np
import torch
from torch.autograd import Variable
import torch.nn.functional as F
import os
from torch.utils.data.sampler import BatchSampler, SubsetRandomSampler
import time
import sys


class PPOAgent(object):

    def __init__(self, env, model, t_max=5, n_worker=1, gamma=0.99, gae_lambda=0.95,
                 ppo_epoch=4, epsilon=0.2, batch_size=32, use_cuda=torch.cuda.is_available()):

        self.env = env

        self.model = model

        self.t_max = t_max
        self.n_worker = n_worker
        self.gamma = gamma

        self.action_tensor = torch.LongTensor
        self.gae_lambda = gae_lambda
        self.ppo_epoch = ppo_epoch
        self.epsilon = epsilon
        self.batch_size = batch_size
        self.use_cuda = use_cuda

    # adapted from https://github.com/ethancaballero/pytorch-a2c-ppo/blob/master/main.py
    def train(self, max_updates=5000, log_writer=None, log_interval=100, evaluator=None, eval_interval=5000,
              lr_scheduler=None, score_name=None, high_is_better=False, dump_interval=100000, dump_dir=None):

        # activate training mode
        self.model.net.train()

        # get the shape of all observations and create a list containing a tensor for all of them
        observation_shapes = [space.shape for space in self.env.observation_space.spaces]
        observations = [torch.zeros(self.t_max + 1, self.n_worker, *[int(x) for x in list(obs_shape)]) for obs_shape in observation_shapes]

        # store first observation
        first_observations = self.env.reset()
        for i, obs in enumerate(first_observations):
            observations[i][0].copy_(torch.from_numpy(obs).float())

        rewards = torch.zeros(self.t_max, self.n_worker, 1)
        value_predictions = torch.zeros(self.t_max + 1, self.n_worker, 1)

        # we will only store the log probs of the chose actions
        old_log_probs = torch.zeros(self.t_max + 1, self.n_worker, 1)
        returns = torch.zeros(self.t_max + 1, self.n_worker, 1)

        actions = self.action_tensor(self.t_max, self.n_worker)
        masks = torch.zeros(self.t_max, self.n_worker, 1)

        # reward bookkeeping
        episode_rewards = torch.zeros([self.n_worker, 1])
        final_rewards = torch.zeros([self.n_worker, 1])

        # best score for model evaluation
        best_score = -np.inf if high_is_better else np.inf

        if self.use_cuda:
            observations = [obs.cuda() for obs in observations]
            rewards = rewards.cuda()
            value_predictions = value_predictions.cuda()
            old_log_probs = old_log_probs.cuda()
            returns = returns.cuda()
            actions = actions.cuda()
            masks = masks.cuda()

        # some timing and logging
        steps = 0
        now = after = time.time()
        step_times = np.ones(11, dtype=np.float32)

        for i in range(1, max_updates+1):

            # estimate updates per second (running avg)
            step_times[0:-1] = step_times[1::]
            step_times[-1] = time.time() - after
            ups = 1.0 / step_times.mean()
            after = time.time()
            print("update %d @ %.1fups" % (np.mod(i, log_interval), ups), end="\r")
            sys.stdout.flush()

            for step in range(self.t_max):
                steps += 1

                policy, value = self.model([Variable(obs[step]) for obs in observations])

                actions[step], np_actions = self._sample_action(policy)

                log_probs = self._get_log_probs(policy, Variable(actions[step].unsqueeze(1))).data

                state, reward, done, _ = self.env.step(np_actions)

                # create a list of state tensors and be sure that they are of type float
                # change shape to the observation shape if necessary (Pendulum-v0)
                state_tensor_list = [torch.from_numpy(s).float().cuda().view(observations[idx].shape[1:])
                                     if self.use_cuda else torch.from_numpy(s).float().view(observations[idx].shape[1:])
                                     for idx, s in enumerate(state)]

                reward = torch.from_numpy(reward).float().view(rewards.shape[1:])
                episode_rewards += reward

                np_masks = np.array([0.0 if done_ else 1.0 for done_ in done])

                # If done then clean the current observation
                for j in range(len(state_tensor_list)):

                    # build mask for current part of observation
                    pt_masks = torch.from_numpy(np_masks.reshape(np_masks.shape[0], *[1 for _ in range(
                        len(state_tensor_list[j].shape[1:]))])).float()
                    if self.use_cuda:
                        pt_masks = pt_masks.cuda()

                    state_tensor_list[j] *= pt_masks

                # store observations, values, rewards and masks
                for j, obs in enumerate(state_tensor_list):
                    observations[j][step + 1].copy_(obs)

                value_predictions[step].copy_(value.data)
                old_log_probs[step].copy_(log_probs)
                rewards[step].copy_(reward)
                masks[step].copy_(torch.from_numpy(np_masks).unsqueeze(1))

                # bookkeeping of rewards
                final_rewards *= masks[step].cpu()
                final_rewards += (1 - masks[step].cpu()) * episode_rewards
                episode_rewards *= masks[step].cpu()

            # calculate returns
            value_predictions[-1] = self.model.forward_value([Variable(obs[-1]) for obs in observations]).data
            gae = 0
            for step in reversed(range(self.t_max)):

                delta = rewards[step] + self.gamma * value_predictions[step+1] * masks[step] - value_predictions[step]
                gae = delta + self.gamma * self.gae_lambda * masks[step] * gae
                returns[step] = gae + value_predictions[step]

            advantages = returns[:-1] - value_predictions[:-1]
            advantages = (advantages - advantages.mean()) / advantages.std()
            for _ in range(self.ppo_epoch):
                sampler = BatchSampler(SubsetRandomSampler(list(range(self.n_worker * self.t_max))),
                                       self.batch_size * self.n_worker, drop_last=False)

                for indices in sampler:
                    states_batch = [obs[:-1].view(-1, *obs.size()[2:])[indices] for obs in observations]

                    actions_batch = actions.view(-1, 1)[indices]
                    return_batch = returns[:-1].view(-1, 1)[indices]
                    old_log_probs_batch = old_log_probs.view(-1, *old_log_probs.size()[2:])[indices]

                    policy, values = self.model([Variable(obs) for obs in states_batch])

                    action_log_probabilities = self._get_log_probs(policy, Variable(actions_batch))

                    ratio = torch.exp(action_log_probabilities - Variable(old_log_probs_batch))

                    advantage_target = Variable(advantages.view(-1, 1)[indices])

                    surr1 = ratio * advantage_target
                    surr2 = ratio.clamp(1.0 - self.epsilon, 1.0 + self.epsilon) * advantage_target

                    action_loss = -torch.min(surr1, surr2).mean()

                    dist_entropy = self._calc_entropy(policy)

                    value_loss = (Variable(return_batch) - values).pow(2).mean()

                    self.model.update([action_loss, value_loss, dist_entropy])

            # save latest state
            for j, obs in enumerate(observations):
                observations[j][0].copy_(obs[-1])

            # logging
            if i % log_interval == 0:
                print("Updates {} ({:.1f}s),  mean/median reward {:.1f}/{:.1f}, entropy {:.5f}, value loss {:.5f},"
                      " policy loss {:.5f}".format(i, time.time()-now, final_rewards.mean(), final_rewards.median(),
                                                   -dist_entropy.data[0], value_loss.data[0], action_loss.data[0]))
                now = time.time()

            if log_writer is not None and i % log_interval == 0:
                log_writer.add_scalar('training/avg_reward', final_rewards.mean(), int(i/log_interval))
                log_writer.add_scalar('training/policy_loss', action_loss, int(i/log_interval))
                log_writer.add_scalar('training/value_loss', value_loss, int(i/log_interval))
                log_writer.add_scalar('training/entropy', -dist_entropy, int(i/log_interval))
                log_writer.add_scalar('training/learn_rate', self.model.optimizer.param_groups[0]['lr'],
                                      int(i/log_interval))
                log_writer.add_scalar('training/steps', steps, int(i / log_interval))

            if evaluator is not None and i % eval_interval == 0:
                self.model.net.eval()
                stats = evaluator.evaluate(self, log_writer, int(i / eval_interval))
                self.model.net.train()

                if score_name is not None:
                    if lr_scheduler is not None:
                        lr_scheduler.step(stats[score_name])
                        if self.model.optimizer.param_groups[0]['lr'] == 0:
                            print('Training stopped')
                            break

                    improvement = (high_is_better and stats[score_name] >= best_score) or \
                                  (not high_is_better and stats[score_name] <= best_score)

                    if improvement:
                        print('New best model at update {}'.format(i))
                        self.store_model('best_model.pt', dump_dir)
                        best_score = stats[score_name]

            if i % dump_interval == 0:
                print('Saved model at update {}'.format(i))
                self.store_model('model_update_{}.pt'.format(i), dump_dir)

    def _calc_entropy(self, policy):
        probabilities = F.softmax(policy, dim=-1)
        log_probs = F.log_softmax(policy, dim=-1)
        return -(log_probs * probabilities).sum(-1).mean()

    def _get_log_probs(self, policy, actions):
        log_probs = F.log_softmax(policy, dim=-1)
        return log_probs.gather(1, actions)

    def _sample_action(self, policy):

        probabilities = F.softmax(policy, dim=-1)

        actions = probabilities.multinomial().data.cpu()

        return actions, actions.squeeze(1).cpu().numpy()

    def perform_action(self, state):

        state = [Variable(torch.from_numpy(s).cuda().float().unsqueeze(0)) if self.use_cuda
                 else Variable(torch.from_numpy(s).float().unsqueeze(0)) for s in state]

        policy = self.model.forward_policy(state)

        # 1 because we want to get the numpy array and 0 because we have to unpack the value
        return self._sample_action(policy)[1][0]

    def store_model(self, name, store_dir=None):

        if store_dir is not None:
            model_path = os.path.join(store_dir, name)
        else:
            model_path = name

        self.model.save_network(model_path)