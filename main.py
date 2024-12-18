import numpy as np
import torch
import gym
import argparse
import os
import d4rl
import time
import wandb
import uuid
import warnings
import subprocess
import torch.nn as nn
import torch.nn.functional as F
from torch.distributions import Normal
from torch.distributions.transformed_distribution import TransformedDistribution
from torch.distributions.transforms import TanhTransform

# Used for clipping
MEAN_MIN = -9.0
MEAN_MAX = 9.0
LOG_STD_MIN = -5
LOG_STD_MAX = 2
LOG_PI_NORM_MAX = 10
LOG_PI_NORM_MIN = -20

EPS = 1e-7


# W&B
def wandb_init(config) -> None:
    env = config.env_s.split('-')[0].capitalize()
    imperfect = config.env_s.split('-')[1].capitalize()
    expert = config.env_e.split('-')[1].capitalize()
    name = f"{str(uuid.uuid4())[:8]}"
    wandb.init(
        config=config,
        project="iLID",
        group=f"{env}-{expert}_{config.num_e}-{imperfect}_{config.num_s_s}_{config.num_s_e}",
        name=f"Seed_{config.seed}-{name}",
        id=str(uuid.uuid4()),
    )
    wandb.run.save()


# Select free GPU
def select_free_device():
    # Run nvidia-smi command to get GPU status
    smi_output = subprocess.check_output(['nvidia-smi',
                                          '--query-gpu=index,utilization.gpu,memory.total,memory.used,memory.free',
                                          '--format=csv'])

    # Convert output to string and split by lines
    smi_output = smi_output.decode('utf-8')
    smi_output_lines = smi_output.strip().split('\n')[1:]

    # Parse GPU status and find the index of the least utilized GPU
    free_gpu_index = None
    free_memory = 0
    for line in smi_output_lines:
        index, gpu_util, memory_total, memory_used, memory_free = line.strip().split(', ')
        # memory_used = int(memory_used.replace(' MiB', ''))
        memory_free = int(memory_free.replace(' MiB', ''))
        if free_gpu_index is None or memory_free > free_memory:
            free_gpu_index = int(index)
            free_memory = memory_free

    # Set the selected GPU as the default device for PyTorch
    device = torch.device(f'cuda:{free_gpu_index}' if torch.cuda.is_available() else 'cpu')
    torch.cuda.set_device(device)

    print(f'Selected GPU {free_gpu_index} with {free_memory} MiB free memory.')

    return device


# Returns D_e and D_s
def get_datasets(dataset_e_raw, dataset_s_raw, num_e=1, num_s_e=10, num_s_s=1000):
    dataset_s = dataset_m_trajs(dataset_s_raw, num_s_s)
    dataset_s['flag'] = np.zeros_like(dataset_s['terminals'])
    dataset_e, dataset_s_extra = dataset_split_expert(dataset_e_raw, num_s_e, num_e + num_s_e)
    dataset_e['flag'] = np.ones_like(dataset_e['terminals'])
    # Add additional expert trajs to D_s
    if dataset_s_extra != {}:
        dataset_s_extra['flag'] = np.ones_like(dataset_s_extra['terminals'])
        for key in dataset_s.keys():
            dataset_s[key] = np.concatenate([dataset_s[key], dataset_s_extra[key]], 0)
    return dataset_e, dataset_s


# Select expert trajs for D_e and D_s
def dataset_split_expert(dataset, split_x, exp_num, terminate_on_end=False):
    n = dataset['rewards'].shape[0]
    return_traj = []
    obs_traj = [[]]
    next_obs_traj = [[]]
    action_traj = [[]]
    reward_traj = [[]]
    done_traj = [[]]
    timeout_traj = [[]]

    for i in range(n - 1):
        obs_traj[-1].append(dataset['observations'][i].astype(np.float32))
        next_obs_traj[-1].append(dataset['observations'][i + 1].astype(np.float32))
        action_traj[-1].append(dataset['actions'][i].astype(np.float32))
        reward_traj[-1].append(dataset['rewards'][i].astype(np.float32))
        done_traj[-1].append(bool(dataset['terminals'][i]))
        timeout_traj[-1].append(bool(dataset['timeouts'][i]))

        final_timestep = dataset['timeouts'][i] | dataset['terminals'][i]
        if (not terminate_on_end) and final_timestep:
            # Skip this transition and don't apply terminals on the last step of an episode
            return_traj.append(np.sum(reward_traj[-1]))
            obs_traj.append([])
            next_obs_traj.append([])
            action_traj.append([])
            reward_traj.append([])
            done_traj.append([])
            timeout_traj.append([])

    # Select trajs
    inds_all = list(range(len(obs_traj)))
    inds_succ = inds_all[:exp_num]
    inds_s = inds_succ[-split_x:] if split_x > 0 else []
    inds_s = list(inds_s)
    inds_succ = list(inds_succ)
    inds_e = set(inds_succ) - set(inds_s)
    inds_e = list(inds_e)

    print('# {} expert trajs in D_e'.format(len(inds_e)))
    print('# {} expert trajs in D_s'.format(len(inds_s)))

    obs_traj_e = [obs_traj[i] for i in inds_e]
    next_obs_traj_e = [next_obs_traj[i] for i in inds_e]
    action_traj_e = [action_traj[i] for i in inds_e]
    reward_traj_e = [reward_traj[i] for i in inds_e]
    done_traj_e = [done_traj[i] for i in inds_e]
    timeout_traj_e = [timeout_traj[i] for i in inds_e]

    obs_traj_s = [obs_traj[i] for i in inds_s]
    next_obs_traj_s = [next_obs_traj[i] for i in inds_s]
    action_traj_s = [action_traj[i] for i in inds_s]
    reward_traj_s = [reward_traj[i] for i in inds_s]
    done_traj_s = [done_traj[i] for i in inds_s]
    timeout_traj_s = [timeout_traj[i] for i in inds_s]

    def concat_trajectories(trajectories):
        return np.concatenate(trajectories, 0)

    dataset_e = {
        'observations': concat_trajectories(obs_traj_e),
        'actions': concat_trajectories(action_traj_e),
        'next_observations': concat_trajectories(next_obs_traj_e),
        'rewards': concat_trajectories(reward_traj_e),
        'terminals': concat_trajectories(done_traj_e),
        'timeouts': concat_trajectories(timeout_traj_e),
    }

    dataset_s = {
        'observations': concat_trajectories(obs_traj_s),
        'actions': concat_trajectories(action_traj_s),
        'next_observations': concat_trajectories(next_obs_traj_s),
        'rewards': concat_trajectories(reward_traj_s),
        'terminals': concat_trajectories(done_traj_s),
        'timeouts': concat_trajectories(timeout_traj_s),
    } if obs_traj_s != [] else {}

    return dataset_e, dataset_s


# Returns m trajs from dataset
def dataset_m_trajs(dataset, m, terminate_on_end=False):
    n = dataset['rewards'].shape[0]
    return_traj = []
    obs_traj = [[]]
    next_obs_traj = [[]]
    action_traj = [[]]
    reward_traj = [[]]
    done_traj = [[]]
    timeout_traj = [[]]

    for i in range(n - 1):
        obs_traj[-1].append(dataset['observations'][i].astype(np.float32))
        next_obs_traj[-1].append(dataset['observations'][i + 1].astype(np.float32))
        action_traj[-1].append(dataset['actions'][i].astype(np.float32))
        reward_traj[-1].append(dataset['rewards'][i].astype(np.float32))
        done_traj[-1].append(bool(dataset['terminals'][i]))
        timeout_traj[-1].append(bool(dataset['timeouts'][i]))

        final_timestep = dataset['timeouts'][i] | dataset['terminals'][i]
        if (not terminate_on_end) and final_timestep:
            # Skip this transition and don't apply terminals on the last step of an episode
            return_traj.append(np.sum(reward_traj[-1]))
            obs_traj.append([])
            next_obs_traj.append([])
            action_traj.append([])
            reward_traj.append([])
            done_traj.append([])
            timeout_traj.append([])

    # Select m trajs
    inds_all = list(range(len(obs_traj)))
    inds = inds_all[:m]
    inds = list(inds)

    print('# {} low-quality trajs in D_s'.format(m))

    obs_traj = [obs_traj[i] for i in inds]
    next_obs_traj = [next_obs_traj[i] for i in inds]
    action_traj = [action_traj[i] for i in inds]
    reward_traj = [reward_traj[i] for i in inds]
    done_traj = [done_traj[i] for i in inds]
    timeout_traj = [timeout_traj[i] for i in inds]

    def concat_trajectories(trajectories):
        return np.concatenate(trajectories, 0)

    return {
        'observations': concat_trajectories(obs_traj),
        'actions': concat_trajectories(action_traj),
        'next_observations': concat_trajectories(next_obs_traj),
        'rewards': concat_trajectories(reward_traj),
        'terminals': concat_trajectories(done_traj),
        'timeouts': concat_trajectories(timeout_traj),
    }


# Define replay buffer for training
class ReplayBuffer(object):
    def __init__(self, state_dim, action_dim, device, max_size=int(1e6)):
        self.device = device

        self.max_size = max_size
        self.ptr = 0
        self.size = 0

        self.state = torch.zeros((max_size, state_dim), device=self.device)
        self.action = torch.zeros((max_size, action_dim), device=self.device)
        self.next_state = torch.zeros((max_size, state_dim), device=self.device)
        self.reward = torch.zeros((max_size, 1), device=self.device)
        self.not_done = torch.zeros((max_size, 1), device=self.device)
        self.flag = torch.zeros((max_size, 1), device=self.device)
        self.weight = torch.ones((max_size, 1), device=self.device)
        self.timeout = torch.ones((max_size, 1), device=self.device)

    def sample(self, batch_size):
        ind = torch.randint(0, self.size, (batch_size,), device=self.device)

        return (
            self.state[ind],
            self.action[ind],
            self.next_state[ind],
            self.reward[ind],
            self.not_done[ind],
            self.flag[ind],
            self.weight[ind],
            self.timeout[ind],
        )

    def convert_d4rl(self, dataset):
        self.state = torch.FloatTensor(dataset['observations']).to(self.device)
        self.action = torch.FloatTensor(dataset['actions']).to(self.device)
        self.next_state = torch.FloatTensor(dataset['next_observations']).to(self.device)
        self.reward = torch.FloatTensor(dataset['rewards'].reshape(-1, 1)).to(self.device)
        self.not_done = torch.FloatTensor(1. - dataset['terminals'].reshape(-1, 1)).to(self.device)
        self.flag = torch.FloatTensor(dataset['flag'].reshape(-1, 1)).to(self.device)
        self.timeout = torch.FloatTensor(dataset['timeouts'].reshape(-1, 1)).to(self.device)
        self.weight = torch.ones_like(self.reward).to(self.device)
        self.size = self.state.shape[0]

    def normalize_states(self, eps=1e-3, mean=None, std=None):
        mean = torch.FloatTensor(mean).to(self.device)
        std = torch.FloatTensor(std).to(self.device)
        if mean is None and std is None:
            mean = self.state.mean(0, keepdims=True)
            std = self.state.std(0, keepdims=True) + eps
        self.state = (self.state - mean) / std
        self.next_state = (self.next_state - mean) / std
        return mean, std

    def add_transitions(self, replay_buffer):
        self.state = torch.cat((self.state, replay_buffer.state), 0)
        self.action = torch.cat((self.action, replay_buffer.action), 0)
        self.next_state = torch.cat((self.next_state, replay_buffer.next_state), 0)
        self.reward = torch.cat((self.reward, replay_buffer.reward), 0)
        self.not_done = torch.cat((self.not_done, replay_buffer.not_done), 0)
        self.flag = torch.cat((self.flag, replay_buffer.flag), 0)
        self.weight = torch.cat((self.weight, replay_buffer.weight), 0)
        self.timeout = torch.cat((self.timeout, replay_buffer.timeout), 0)
        self.size = self.state.shape[0]


# Define actor model
class Actor(nn.Module):
    def __init__(self, state_dim, action_dim):
        super(Actor, self).__init__()

        self.fc1 = nn.Linear(state_dim, 256)
        self.fc2 = nn.Linear(256, 256)
        self.mu_head = nn.Linear(256, action_dim)
        self.sigma_head = nn.Linear(256, action_dim)

    def _get_outputs(self, state):
        a = F.relu(self.fc1(state))
        a = F.relu(self.fc2(a))
        mu = self.mu_head(a)
        mu = torch.clip(mu, MEAN_MIN, MEAN_MAX)
        log_sigma = self.sigma_head(a)
        log_sigma = torch.clip(log_sigma, LOG_STD_MIN, LOG_STD_MAX)
        sigma = torch.exp(log_sigma)

        a_distribution = TransformedDistribution(Normal(mu, sigma), TanhTransform(cache_size=1))
        a_tanh_mode = torch.tanh(mu)
        return a_distribution, a_tanh_mode

    def forward(self, state):
        a_dist, a_tanh_mode = self._get_outputs(state)
        action = a_dist.rsample()
        logp_pi = a_dist.log_prob(action).sum(axis=-1)
        return action, logp_pi, a_tanh_mode

    def get_log_density(self, state, action):
        a_dist, _ = self._get_outputs(state)
        action_clip = torch.clip(action, -1. + EPS, 1. - EPS)
        logp_action = a_dist.log_prob(action_clip)
        return logp_action


# Define discriminator model
class Discriminator(nn.Module):
    def __init__(self, state_dim):
        super(Discriminator, self).__init__()

        self.fc1 = nn.Linear(state_dim, 256)
        self.fc2 = nn.Linear(256, 256)
        self.fc3 = nn.Linear(256, 1)

    def forward(self, state):
        d = F.relu(self.fc1(state))
        d = F.relu(self.fc2(d))
        d = torch.sigmoid(self.fc3(d))
        # For a fair comparison to DWBC (https://proceedings.mlr.press/v162/xu22l/xu22l.pdf)
        d = torch.clip(d, 0.1, 0.9)
        return d


# Define scalar model for alpha
class Scalar(nn.Module):
    def __init__(self, init_value: float):
        super().__init__()
        self.constant = nn.Parameter(torch.tensor(init_value, dtype=torch.float32))

    def forward(self) -> nn.Parameter:
        return self.constant


# Define algorithm model
class iLID(object):
    def __init__(
            self,
            state_dim,
            action_dim,
            device,
            no_pu=False,
            eta=0.5,
            d_steps=100_000,
            policy_lr=1e-5,
            regularization=0.005,
            alpha=1.0,
            automatic_alpha_tuning=True,
            epsilon=0.01
    ):
        self.device = device

        self.policy = Actor(state_dim, action_dim).to(self.device)
        self.policy_optimizer = torch.optim.Adam(self.policy.parameters(), lr=policy_lr, weight_decay=regularization)

        self.discriminator = Discriminator(state_dim).to(self.device)
        self.discriminator_optimizer = torch.optim.Adam(self.discriminator.parameters(), lr=1e-5, weight_decay=0.005)

        self.d_steps = d_steps
        self.no_pu_learning = no_pu
        self.eta = eta

        self.alpha = alpha

        self.log_policy_e = None

        # Automatic alpha tuning
        self.automatic_alpha_tuning = automatic_alpha_tuning
        if self.automatic_alpha_tuning:
            self.epsilon = epsilon
            self.log_alpha = Scalar(0.0)
            self.alpha_optimizer = torch.optim.Adam(self.log_alpha.parameters(), lr=policy_lr)

            self.policy_e = Actor(state_dim, action_dim).to(self.device)
            self.policy_e_optimizer = torch.optim.Adam(self.policy_e.parameters(),
                                                       lr=policy_lr,
                                                       weight_decay=regularization)

        self.total_it = 0
        self.total_it_bc = 0

    def alpha_and_alpha_loss(self, log_pi, log_pi_e):
        alpha_loss = self.log_alpha().exp() * (torch.mean(log_pi) + self.epsilon - torch.mean(log_pi_e)).detach()
        alpha = self.log_alpha().exp()
        return alpha, alpha_loss

    def select_action(self, state, is_policy_e=False):
        state = torch.FloatTensor(state.reshape(1, -1)).to(self.device)
        _, _, action = self.policy_e(state) if is_policy_e else self.policy(state)
        return action.cpu().data.numpy().flatten()

    def train_discriminator(self, replay_buffer_e, replay_buffer_s, batch_size=256):
        for t in range(int(self.d_steps)):
            # Sample states from D_e and D_s
            state_e, _, _, _, _, _, _, _ = replay_buffer_e.sample(batch_size)
            state_s, _, _, _, _, _, _, _ = replay_buffer_s.sample(batch_size)

            # Compute discriminator loss
            d_e = self.discriminator(state_e)
            d_s = self.discriminator(state_s)
            if self.no_pu_learning:
                d_loss_e = -torch.log(d_e)
                d_loss_s = -torch.log(1 - d_s)
                d_loss = torch.mean(d_loss_e + d_loss_s)
            else:
                d_loss_e = -torch.log(d_e)
                d_loss_s = -torch.log(1 - d_s) / self.eta + torch.log(1 - d_e)
                d_loss = torch.mean(d_loss_e + d_loss_s)

            # Optimize the discriminator
            self.discriminator_optimizer.zero_grad()
            d_loss.backward()
            self.discriminator_optimizer.step()

            if (t + 1) % 5000 == 0:
                print(f"Discriminator loss ({t + 1}/{int(self.d_steps)}): {d_loss:.3f}")

    def select_data(self, replay_buffer_s, bar, rollback=1, decay=.5, weight_init=1.0):
        # Select the transitions that next_state is similar to expert states
        next_state = replay_buffer_s.next_state
        mask = torch.squeeze(self.discriminator(next_state) >= bar)
        # Anchoring trajectory positions
        replay_buffer_s.not_done[-1] = 0
        done = torch.where((replay_buffer_s.not_done == 0) | (replay_buffer_s.timeout == 1))[0] + 1
        state = replay_buffer_s.state
        # Ensure each weight equals to zero
        replay_buffer_s.weight -= 1

        # Weight decay
        weight_decay = weight_init
        for k in range(0, rollback):
            index = torch.squeeze(self.discriminator(state) >= bar)  # Mask for current states
            start = 0
            for end in done:
                index[start: min(end, start + k + 1)] = False  # Let indexes be False if they will move before dones
                start = end
            mask[:-(k + 1)] |= index[k + 1:]
            replay_buffer_s.weight[mask & (torch.squeeze(replay_buffer_s.weight) < weight_decay), :] = weight_decay
            weight_decay = weight_decay * decay

        replay_buffer_s.state = replay_buffer_s.state[mask, :]
        replay_buffer_s.action = replay_buffer_s.action[mask, :]
        replay_buffer_s.next_state = replay_buffer_s.next_state[mask, :]
        replay_buffer_s.reward = replay_buffer_s.reward[mask, :]
        replay_buffer_s.not_done = replay_buffer_s.not_done[mask, :]
        replay_buffer_s.flag = replay_buffer_s.flag[mask, :]
        replay_buffer_s.weight = replay_buffer_s.weight[mask, :]
        replay_buffer_s.size = replay_buffer_s.state.shape[0]
        return replay_buffer_s

    def train_policy(self, replay_buffer_s, replay_buffer_e, batch_size=256):
        self.total_it += 1

        # Sample from D_e and D_s
        minibatch = batch_size
        state_s, action_s, _, _, _, _, weight_s, _ = replay_buffer_s.sample(minibatch)
        state_e, action_e, _, _, _, _, weight_e, _ = replay_buffer_e.sample(minibatch)

        # Compute log_prob
        log_pi_s = self.policy.get_log_density(state_s, action_s)
        log_pi_e = self.policy.get_log_density(state_e, action_e)

        # Update alpha
        if self.automatic_alpha_tuning:
            if self.log_policy_e is not None:
                log_pi_e_e = self.log_policy_e
            else:
                log_pi_e_e = self.policy_e.get_log_density(state_e, action_e)

            self.alpha, alpha_loss = self.alpha_and_alpha_loss(torch.sum(log_pi_e, 1), torch.sum(log_pi_e_e, 1))
            self.alpha_optimizer.zero_grad()
            alpha_loss.backward()
            self.alpha_optimizer.step()

        # Compute policy loss
        p_loss = torch.mean(-torch.sum(log_pi_s, 1) * weight_s) + self.alpha * torch.mean(-torch.sum(log_pi_e, 1))

        # Optimize the policy
        self.policy_optimizer.zero_grad()
        p_loss.backward()
        self.policy_optimizer.step()

        return p_loss

    def train_policy_e(self, replay_buffer_e, batch_size=256):
        self.total_it_bc += 1

        # Sample from D_e
        state_e, action_e, _, _, _, _, weight_e, _ = replay_buffer_e.sample(batch_size)

        # Compute log_prob
        log_pi_e = self.policy_e.get_log_density(state_e, action_e)

        # Compute policy loss
        p_loss_bc = torch.mean(-torch.sum(log_pi_e, 1))

        # Optimize the policy
        self.policy_e_optimizer.zero_grad()
        p_loss_bc.backward()
        self.policy_e_optimizer.step()

        return p_loss_bc

    def save(self, filename):
        torch.save(self.discriminator.state_dict(), filename + "_discriminator")
        torch.save(self.discriminator_optimizer.state_dict(), filename + "_discriminator_optimizer")

        torch.save(self.policy.state_dict(), filename + "_policy")
        torch.save(self.policy_optimizer.state_dict(), filename + "_policy_optimizer")

    def load(self, filename):
        self.discriminator.load_state_dict(torch.load(filename + "_discriminator"))
        self.discriminator_optimizer.load_state_dict(torch.load(filename + "_discriminator_optimizer"))

        self.policy.load_state_dict(torch.load(filename + "_policy"))
        self.policy_optimizer.load_state_dict(torch.load(filename + "_policy_optimizer"))


# Runs policy for eval_episodes episodes and returns D4RL score
# A fixed seed is used for the eval environment
def eval_policy(time_steps, policy, env_name, seed, mean, std, policy_loss, n_selected_data=0, alpha=1.,
                is_policy_e=False, seed_offset=100, eval_episodes=10):
    # Evaluate BC policy or learned policy
    policy.policy_e.eval() if is_policy_e else policy.policy.eval()

    eval_env = gym.make(env_name)
    eval_env.seed(seed + seed_offset)

    # Evaluate policy
    avg_reward = 0.
    for _ in range(eval_episodes):
        state, done = eval_env.reset(), False
        while not done:
            state = (np.array(state).reshape(1, -1) - mean) / std
            action = policy.select_action(state, is_policy_e)
            state, reward, done, _ = eval_env.step(action)
            avg_reward += reward

    avg_reward /= eval_episodes
    d4rl_score = eval_env.get_normalized_score(avg_reward) * 100

    policy.policy_e.train() if is_policy_e else policy.policy.train()

    # wandb info
    if not is_policy_e:
        wandb.log(
            {"average_returns": avg_reward,
             "d4rl_normalized_score": d4rl_score,
             "policy_loss": policy_loss,
             "selected_data": n_selected_data,
             "alpha": alpha,
             },
            step=time_steps)

    print("---------------------------------------")
    print(f"Env: {env_name}, Evaluation over {eval_episodes} episodes: {avg_reward:.3f}, D4RL score: {d4rl_score:.3f}, "
          f"Policy loss:{policy_loss:.3f}")
    print("---------------------------------------")
    return d4rl_score


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    # Experiment
    parser.add_argument("--root_dir", default="results")  # Root dir
    parser.add_argument("--algorithm", default="iLID")  # Algorithm name
    parser.add_argument('--env_e', default="hopper-expert-v2")  # Expert environment
    parser.add_argument('--env_s', default="hopper-random-v2")  # imperfect environment
    parser.add_argument("--num_e", default=1, type=int)  # Expert trajs
    parser.add_argument("--num_s_e", default=10, type=int)  # Expert trajs in the imperfect dataset
    parser.add_argument("--num_s_s", default=1000, type=int)  # Low-quality trajs in the imperfect dataset
    parser.add_argument("--seed", default=0, type=int)  # Sets Gym, PyTorch and Numpy seeds
    parser.add_argument("--eval_freq", default=20000, type=int)  # How often (time steps) we evaluate
    parser.add_argument("--max_timesteps", default=2_000_000, type=int)  # Max time steps for training the policy
    parser.add_argument("--policy_lr", default=1e-5, type=float)  # Policy learning rate
    parser.add_argument("--regularization", default=0.005, type=float)  # Decay for Adam
    parser.add_argument("--batch_size", default=256, type=int)  # Batch size for training
    parser.add_argument("--no_normalize", action='store_true')  # If normalizing states
    # iLID
    parser.add_argument("--d_steps", default=100_000, type=int)  # Max time steps for training the discriminator
    parser.add_argument("--weight_init", default=1., type=float)  # Initial value of the weight
    parser.add_argument("--decay", default=.9, type=float)  # Decay for rollback actions
    parser.add_argument("--rollback", default=5, type=int)  # Rollback steps
    parser.add_argument("--bar", default=0.89, type=float)  # Bar for selecting good data
    parser.add_argument("--alpha", default=1.0, type=float)  # Value of alpha (initialization)
    parser.add_argument("--automatic_alpha_tuning", default=True, type=bool)  # If tuning alpha automatically
    parser.add_argument("--bc_steps", default=200_000, type=int)  # BC steps
    parser.add_argument("--epsilon", default=.01, type=float)  # KL divergence upperbound
    parser.add_argument("--bc_init", default=True, type=bool)  # If using policy_e as initialization
    parser.add_argument("--no_pu", action='store_true')  # If using the PU trick
    parser.add_argument("--eta", default=0.5, type=float)  # Balancing positive-unlabeled learning
    args = parser.parse_args()

    device = select_free_device()  # torch.device('cuda:0')

    # Checkpoint dir
    dataset_name = f"env_e-{args.env_e}_env_s-{args.env_s}_num_e-{args.num_e}_num_s_e-{args.num_s_e}_num_s_s-{args.num_s_s}"
    algo_name = f"{args.algorithm}"
    os.makedirs(f"{args.root_dir}/{dataset_name}/{algo_name}", exist_ok=True)
    save_dir = f"{args.root_dir}/{dataset_name}/{algo_name}/seed-{args.seed}.txt"
    print("---------------------------------------")
    print(f"Dataset: {dataset_name}, Algorithm: {algo_name}, Seed: {args.seed}")
    print("---------------------------------------")

    # Make environments
    env_e = gym.make(args.env_e)
    env_id = args.env_e.split('-')[0]
    env_s = gym.make(args.env_s)

    # Set seeds
    env_e.seed(args.seed)
    env_e.action_space.seed(args.seed)
    env_s.seed(args.seed)
    env_s.action_space.seed(args.seed)
    torch.manual_seed(args.seed)
    np.random.seed(args.seed)

    # Record dimensions
    state_dim = env_e.observation_space.shape[0]
    action_dim = env_e.action_space.shape[0]

    # Initialize policy
    policy = iLID(state_dim,
                  action_dim,
                  device,
                  no_pu=args.no_pu,
                  eta=args.eta,
                  d_steps=args.d_steps,
                  policy_lr=args.policy_lr,
                  regularization=args.regularization,
                  alpha=args.alpha,
                  automatic_alpha_tuning=args.automatic_alpha_tuning,
                  epsilon=args.epsilon)

    # Load dataset
    dataset_e_raw = env_e.get_dataset()
    dataset_s_raw = env_s.get_dataset()
    dataset_e, dataset_s = get_datasets(
        dataset_e_raw, dataset_s_raw, args.num_e, args.num_s_e, args.num_s_s
    )

    # Build replay buffers
    states_e = dataset_e['observations']
    states_s = dataset_s['observations']
    states_o = np.concatenate([states_e, states_s]).astype(np.float32)
    replay_buffer_e = ReplayBuffer(state_dim, action_dim, device)
    replay_buffer_s = ReplayBuffer(state_dim, action_dim, device)
    replay_buffer_e.convert_d4rl(dataset_e)
    replay_buffer_s.convert_d4rl(dataset_s)
    print('# {} of expert demonstrations'.format(states_e.shape[0]))
    print('# {} of imperfect demonstrations'.format(states_s.shape[0]))

    # Normalize states
    if args.no_normalize:
        shift, scale = 0, 1
    else:
        shift = np.mean(states_o, 0)
        scale = np.std(states_o, 0) + 1e-3
    replay_buffer_e.normalize_states(mean=shift, std=scale)
    replay_buffer_s.normalize_states(mean=shift, std=scale)

    # wandb info
    wandb_init(args)

    eval_log = open(save_dir, 'w')

    # Warnings
    if replay_buffer_e.size >= 10000:
        warnings.warn(f"# expert state-actions: {replay_buffer_e.size}, consider increasing d_steps & bc-steps!",
                      UserWarning)

    # Train discriminator
    policy.train_discriminator(replay_buffer_e, replay_buffer_s, args.batch_size)

    # Select good state-actions from imperfect demonstrations
    replay_buffer_s = policy.select_data(replay_buffer_s, args.bar, args.rollback, args.decay, args.weight_init)
    n_selected_data = replay_buffer_s.size
    print(f"# selected positive imperfect state-actions: {n_selected_data}")

    # Add expert data into the imperfect dataset
    replay_buffer_s.add_transitions(replay_buffer_e)
    print(f"# training state-actions: {replay_buffer_s.size}")

    # Train BC policy
    if args.automatic_alpha_tuning:
        for t in range(int(args.bc_steps)):
            policy_e_loss = policy.train_policy_e(replay_buffer_e)
            # Evaluate BC policy
            if (t == 0) | ((t + 1) % args.eval_freq == 0):
                print(f"(BC) Time steps: {t + 1}")
                average_returns = eval_policy(t + 1, policy, args.env_e, args.seed, shift, scale, policy_e_loss,
                                              is_policy_e=True, n_selected_data=n_selected_data, alpha=policy.alpha)

        # Use policy_e to initialize imitation policy
        if args.bc_init:
            policy.policy.load_state_dict(policy.policy_e.state_dict())

        # Compute the expected entropy w.r.t. policy_e
        state_e, action_e, _, _, _, _, _, _ = replay_buffer_e.sample(replay_buffer_e.size)
        policy.log_policy_e = policy.policy_e.get_log_density(state_e, action_e)

    # Train imitation policy
    for t in range(int(args.max_timesteps)):
        policy_loss = policy.train_policy(replay_buffer_s, replay_buffer_e)
        # Evaluate imitation policy
        if (t == 0) | ((t + 1) % args.eval_freq == 0):
            print(f"(iLID) Time steps: {t + 1}")
            average_returns = eval_policy(t + 1, policy, args.env_e, args.seed, shift, scale, policy_loss,
                                          n_selected_data=n_selected_data, alpha=policy.alpha)
            eval_log.write(f'{t + 1}\t{average_returns}\n')
            eval_log.flush()
    eval_log.close()
