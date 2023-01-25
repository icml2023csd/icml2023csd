from collections import deque

import numpy as np
import pickle
from mujoco_py import MujocoException

from baselines.her.util import convert_episode_to_batch_major, store_args

class RolloutWorker:

    @store_args
    def __init__(self, make_env, policy, dims, logger, T, rollout_batch_size=1,
                 exploit=False, use_target_net=False, compute_Q=False, noise_eps=0,
                 random_eps=0, history_len=100, render=False, **kwargs):
        """Rollout worker generates experience by interacting with one or many environments.

        Args:
            make_env (function): a factory function that creates a new instance of the environment
                when called
            policy (object): the policy that is used to act
            dims (dict of ints): the dimensions for observations (o), goals (g), and actions (u)
            logger (object): the logger that is used by the rollout worker
            rollout_batch_size (int): the number of parallel rollouts that should be used
            exploit (boolean): whether or not to exploit, i.e. to act optimally according to the
                current policy without any exploration
            use_target_net (boolean): whether or not to use the target net for rollouts
            compute_Q (boolean): whether or not to compute the Q values alongside the actions
            noise_eps (float): scale of the additive Gaussian noise
            random_eps (float): probability of selecting a completely random action
            history_len (int): length of history for statistics smoothing
            render (boolean): whether or not to render the rollouts
        """
        self.envs = [make_env() for _ in range(rollout_batch_size)]
        assert self.T > 0

        self.info_keys = [key.replace('info_', '') for key in dims.keys() if key.startswith('info_')]

        self.success_history = deque(maxlen=history_len)
        self.Q_history = deque(maxlen=history_len)
        self.episode_length_history = deque(maxlen=history_len)
        self.once_success_history = deque(maxlen=history_len)
        self.return_history = deque(maxlen=history_len)

        self.n_episodes = 0
        self.g = np.empty((self.rollout_batch_size, self.dims['g']), np.float32)  # goals
        self.initial_o = np.empty((self.rollout_batch_size, self.dims['o']), np.float32)  # observations
        self.initial_ag = np.empty((self.rollout_batch_size, self.dims['g']), np.float32)  # achieved goals
        self.reset_all_rollouts()
        self.clear_history()

    def reset_rollout(self, i, generated_goal):
        """Resets the `i`-th rollout environment, re-samples a new goal, and updates the `initial_o`
        and `g` arrays accordingly.
        """
        obs = self.envs[i].reset()
        if isinstance(obs, dict):
            self.g[i] = obs['desired_goal']
            if isinstance(generated_goal, np.ndarray):
                self.g[i] = self.envs[i].env.goal = generated_goal[i].copy()
            self.initial_o[i] = obs['observation']
            self.initial_ag[i] = obs['achieved_goal']
        else:
            self.g[i] = np.zeros_like(self.g[i])
            self.initial_o[i] = obs
            self.initial_ag[i] = np.zeros_like(self.initial_ag[i])

    def reset_all_rollouts(self, generated_goal=False):
        """Resets all `rollout_batch_size` rollout workers.
        """
        for i in range(self.rollout_batch_size):
            self.reset_rollout(i, generated_goal)

    def generate_rollouts(self, generated_goal=False, z_s_onehot=False, random_action=False):
        """Performs `rollout_batch_size` rollouts in parallel for time horizon `T` with the current
        policy acting on it accordingly.
        """
        self.reset_all_rollouts(generated_goal)

        # compute observations
        o = np.empty((self.rollout_batch_size, self.dims['o']), np.float32)  # observations
        ag = np.empty((self.rollout_batch_size, self.dims['g']), np.float32)  # achieved goals
        o[:] = self.initial_o
        ag[:] = self.initial_ag
        z = z_s_onehot.copy()  # selected skills

        # generate episodes
        obs, zs, achieved_goals, acts, goals, successes = [], [], [], [], [], []
        rewards, dones, valids = [], [], []
        HW = 200
        if self.render == 'rgb_array':
            imgs = np.empty([self.rollout_batch_size, self.T, HW, HW, 3])
        elif self.render == 'human':
            imgs = np.empty([self.rollout_batch_size, self.T, 992, 1648, 3])
        info_values = [np.empty((self.T, self.rollout_batch_size, self.dims['info_' + key]), np.float32) for key in self.info_keys]
        Qs = []
        cur_valid = np.ones(self.rollout_batch_size)
        lengths = np.full(self.rollout_batch_size, -1)
        once_successes = np.full(self.rollout_batch_size, 0)
        returns = np.zeros(self.rollout_batch_size)
        for t in range(self.T):
            policy_output = self.policy.get_actions(
                o, z, ag, self.g,
                compute_Q=self.compute_Q,
                noise_eps=self.noise_eps if not self.exploit else 0.,
                random_eps=(self.random_eps if not self.exploit else 0.) if not random_action else 1.,
                use_target_net=self.use_target_net,
                exploit=self.exploit,
            )

            if self.compute_Q:
                u, Q = policy_output
                Qs.append(Q)
            else:
                u = policy_output

            if u.ndim == 1:
                # The non-batched case should still have a reasonable shape.
                u = u.reshape(1, -1)

            o_new = np.empty((self.rollout_batch_size, self.dims['o']))
            ag_new = np.empty((self.rollout_batch_size, self.dims['g']))
            success = np.zeros(self.rollout_batch_size)
            cur_reward = np.zeros(self.rollout_batch_size)
            cur_done = np.zeros(self.rollout_batch_size)
            # compute new states and observations
            for i in range(self.rollout_batch_size):
                try:
                    curr_o_new, reward, done, info = self.envs[i].step(u[i])
                    if 'is_success' in info:
                        success[i] = info['is_success']
                    cur_reward[i] = reward
                    cur_done[i] = done
                    if (done or t == self.T - 1) and lengths[i] == -1:
                        if 'cur_step' in info:
                            lengths[i] = info['cur_step']
                        else:
                            lengths[i] = t + 1
                    if success[i] > 0:
                        once_successes[i] = 1
                    if cur_valid[i]:
                        returns[i] += reward
                    if isinstance(curr_o_new, dict):
                        o_new[i] = curr_o_new['observation']
                        ag_new[i] = curr_o_new['achieved_goal']
                        for idx, key in enumerate(self.info_keys):
                            info_values[idx][t, i] = info[key]
                    else:
                        o_new[i] = curr_o_new
                        ag_new[i] = np.zeros_like(ag_new[i])
                    if self.render:
                        if self.render == 'rgb_array':
                            imgs[i][t] = self.envs[i].render(mode='rgb_array', width=HW, height=HW)
                        elif self.render == 'human':
                            imgs[i][t] = self.envs[i].render()

                except MujocoException as e:
                    return self.generate_rollouts()

            if np.isnan(o_new).any():
                self.logger.warning('NaN caught during rollout generation. Trying again...')
                self.reset_all_rollouts()
                return self.generate_rollouts()

            obs.append(o.copy())
            rewards.append(cur_reward.copy())
            dones.append(cur_done.copy())
            valids.append(cur_valid.copy())
            zs.append(z.copy())
            achieved_goals.append(ag.copy())
            successes.append(success.copy())
            acts.append(u.copy())
            goals.append(self.g.copy())
            o[...] = o_new
            ag[...] = ag_new

            for i in range(len(cur_valid)):
                if cur_done[i]:
                    cur_valid[i] = 0

        # success: success at the last step
        # once_success: once success
        obs.append(o.copy())
        achieved_goals.append(ag.copy())
        self.initial_o[:] = o

        successful = np.array(successes)[-1, :].copy()

        episode = dict(
            o=obs,
            z=zs,
            u=acts,
            g=goals,
            ag=achieved_goals,

            myr=rewards,
            myd=dones,
            myv=valids,
        )
        for key, value in zip(self.info_keys, info_values):
            episode['info_{}'.format(key)] = value

        # stats
        assert successful.shape == (self.rollout_batch_size,)
        success_rate = np.mean(successful)
        self.success_history.append(success_rate)
        self.once_success_history.append(np.mean(once_successes))
        self.return_history.append(np.mean(returns))
        self.episode_length_history.append(np.mean(lengths))
        if self.compute_Q:
            self.Q_history.append(np.mean(Qs))
        self.n_episodes += self.rollout_batch_size

        if self.render == 'rgb_array' or self.render == 'human':
            return imgs, convert_episode_to_batch_major(episode)

        return convert_episode_to_batch_major(episode)

    def clear_history(self):
        """Clears all histories that are used for statistics
        """
        self.success_history.clear()
        self.Q_history.clear()
        self.episode_length_history.clear()
        self.once_success_history.clear()
        self.return_history.clear()

    def current_success_rate(self):
        return np.mean(self.success_history)

    def current_mean_Q(self):
        return np.mean(self.Q_history)

    def save_policy(self, path):
        """Pickles the current policy for later inspection.
        """
        with open(path, 'wb') as f:
            pickle.dump(self.policy, f)

    def logs(self, prefix='worker'):
        """Generates a dictionary that contains all collected statistics.
        """
        logs = []
        logs += [('num_trajs', len(self.success_history) * self.rollout_batch_size)]
        logs += [('success_rate', np.mean(self.success_history))]
        logs += [('once_success_rate', np.mean(self.once_success_history))]
        logs += [('return', np.mean(self.return_history))]
        logs += [('episode_length', np.mean(self.episode_length_history))]
        if self.compute_Q:
            logs += [('mean_Q', np.mean(self.Q_history))]
        logs += [('episode', self.n_episodes)]

        if prefix is not '' and not prefix.endswith('/'):
            return [(prefix + '/' + key, val) for key, val in logs]
        else:
            return logs

    def seed(self, seed):
        """Seeds each environment with a distinct seed derived from the passed in global seed.
        """
        for idx, env in enumerate(self.envs):
            env.seed(seed + 1000 * idx)
