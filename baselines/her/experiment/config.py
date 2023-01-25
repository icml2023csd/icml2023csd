from copy import deepcopy
import numpy as np
import json
import os
import gym

from baselines import logger
from baselines.her.ddpg import DDPG
from baselines.her.her import make_sample_her_transitions


DEFAULT_ENV_PARAMS = {
    'FetchReach-v0': {
        'n_cycles': 10,
    },
}


DEFAULT_PARAMS = {
    # env
    'max_u': 1.,  # max absolute value of actions on different coordinates
    # ddpg
    'layers': 3,  # number of layers in the critic/actor networks
    'hidden': 256,  # number of neurons in each hidden layers
    'network_class_actor_critic': 'baselines.her.actor_critic:ActorCritic',
    'network_class_discriminator': 'baselines.her.discriminator:Discriminator',
    'Q_lr': 0.001,  # critic learning rate
    'pi_lr': 0.001,  # actor learning rate
    'sk_lr': 0.001,  # skill discriminator learning rate
    'buffer_size': int(1E6), 
    'polyak': 0.95,  # polyak averaging coefficient
    'action_l2': 1.0,  # quadratic penalty on actions (before rescaling by max_u)
    'clip_obs': 200.,
    'scope': 'ddpg',  # can be tweaked for testing
    'relative_goals': False,
    # training
    'n_cycles': 50,  # per epoch
    'rollout_batch_size': 2,  # per mpi thread
    'n_batches': 40,  # training batches per cycle
    'batch_size': 256,  # per mpi thread, measured in transitions and reduced to even multiple of chunk_length.
    'n_test_rollouts': 10,  # number of test rollouts per epoch, each consists of rollout_batch_size rollouts
    'test_with_polyak': False,  # run test episodes with the target network
    # exploration
    'random_eps': 0.3,  # percentage of time a random action is taken
    'noise_eps': 0.2,  # std of gaussian noise added to not-completely-random actions as a percentage of max_u
    # normalization
    'norm_eps': 0.01,  # epsilon used for observation normalization
    'norm_clip': 5,  # normalized observations are cropped to this values
    # HER
    'replay_strategy': 'future',

    'collect_data': False, # True: collect rollouts data
    'collect_video': False, # True: collect rollouts video

    'num_objective': 4, # number of rl objectives

###############################################################################

    'note': 'DIAYN',

    'sac': True, # Soft-Actor-Critic
    'replay_k': 0,

    'r_scale': 0,
    'sk_r_scale': 0,
    'et_r_scale': 0.02,

    'goal_generation': 'Zero', # 'Env', 'Zero'

    'num_skills': 1, # number of skills to learn
    'use_skill_n': None, # starts from 1 to num_skills or None

    'et_w_schedule': [(0,0.2),(24,0.2),(200,0.2)], # Entropy regularization coefficient (SAC)

    'finetune_pi': False, # pi: policy 
    'load_weight': None
}


CACHED_ENVS = {}
def cached_make_env(make_env):
    """
    Only creates a new environment from the provided function if one has not yet already been
    created. This is useful here because we need to infer certain properties of the env, e.g.
    its observation and action spaces, without any intend of actually using it.
    """
    if make_env not in CACHED_ENVS:
        env = make_env()
        CACHED_ENVS[make_env] = env
    return CACHED_ENVS[make_env]

def prepare_params(kwargs):
    # DDPG params
    ddpg_params = dict()

    env_name = kwargs['env_name']
    def make_env():
        # Only for getting max_episode_steps
        if env_name == 'Maze':
            from envs.maze_env import MazeEnv
            env = MazeEnv(n=10)
        elif env_name == 'Kitchen':
            from d4rl_alt.kitchen.kitchen_envs import KitchenMicrowaveKettleLightTopLeftBurnerV0Custom
            from gym.wrappers.time_limit import TimeLimit
            env = KitchenMicrowaveKettleLightTopLeftBurnerV0Custom(control_mode='end_effector')
            env = TimeLimit(env, max_episode_steps=kwargs['max_path_length'])
        else:
            env = gym.make(env_name)
            if 'max_path_length' in kwargs:
                env = env.env
                from gym.wrappers.time_limit import TimeLimit
                env = TimeLimit(env, max_episode_steps=kwargs['max_path_length'])
        return env
    kwargs['make_env'] = make_env
    tmp_env = cached_make_env(kwargs['make_env'])
    assert hasattr(tmp_env, '_max_episode_steps')
    kwargs['T'] = tmp_env._max_episode_steps
    tmp_env.reset()
    kwargs['max_u'] = np.array(kwargs['max_u']) if type(kwargs['max_u']) == list else kwargs['max_u']
    kwargs['gamma'] = 1. - 1. / kwargs['T']
    if 'lr' in kwargs:
        kwargs['pi_lr'] = kwargs['lr']
        kwargs['Q_lr'] = kwargs['lr']
        kwargs['sk_lr'] = kwargs['lr']
        del kwargs['lr']
    for name in ['buffer_size', 'hidden', 'layers',
                 'network_class_actor_critic', 'network_class_discriminator',
                 'polyak', 
                 'batch_size', 'Q_lr', 'pi_lr', 'sk_lr',
                 'norm_eps', 'norm_clip', 'max_u',
                 'action_l2', 'clip_obs', 'scope', 'relative_goals']:
        ddpg_params[name] = kwargs[name]
        kwargs['_' + name] = kwargs[name]
        del kwargs[name]
    kwargs['ddpg_params'] = ddpg_params

    return kwargs


def log_params(params, logger=logger):
    for key in sorted(params.keys()):
        logger.info('{}: {}'.format(key, params[key]))


def configure_her(params):
    env = cached_make_env(params['make_env'])
    env.reset()
    def reward_fun(ag_2, g, info):  # vectorized
        return env.compute_reward(achieved_goal=ag_2, desired_goal=g, info=info)

    her_params = {
        'reward_fun': reward_fun,
    }
    for name in ['replay_strategy', 'replay_k', 'et_w_schedule']:
        her_params[name] = params[name]
        params['_' + name] = her_params[name]
        del params[name]

    sample_her_transitions = make_sample_her_transitions(**her_params)

    return sample_her_transitions


def simple_goal_subtract(a, b):
    assert a.shape == b.shape
    return a - b


def configure_ddpg(dims, params, pretrain_weights, reuse=False, use_mpi=True, clip_return=True):
    sample_her_transitions = configure_her(params)
    # Extract relevant parameters.
    gamma = params['gamma']
    rollout_batch_size = params['rollout_batch_size']
    ddpg_params = params['ddpg_params']
    env_name = params['env_name']
    max_timesteps = params['max_timesteps']
    num_objective = params['num_objective']

    input_dims = dims.copy()

    # DDPG agent
    env = cached_make_env(params['make_env'])
    env.reset()
    ddpg_params.update({'input_dims': input_dims,  # agent takes an input observations
                        'T': params['T'],
                        'clip_pos_returns': True,  # clip positive returns
                        'clip_return': (1. / (1. - gamma))*num_objective if clip_return else np.inf,  # max abs of return
                        'rollout_batch_size': rollout_batch_size,
                        'subtract_goals': simple_goal_subtract,
                        'sample_transitions': sample_her_transitions,
                        'gamma': gamma,
                        'env_name': env_name,
                        'max_timesteps': max_timesteps,
                        'r_scale': params['r_scale'],
                        'sk_r_scale': params['sk_r_scale'],
                        'et_r_scale': params['et_r_scale'],
                        'pretrain_weights': pretrain_weights,
                        'finetune_pi': params['finetune_pi'],
                        'sac': params['sac'],
                        'skill_type': params['skill_type'],
                        'sk_clip': params['sk_clip'],
                        'et_clip': params['et_clip'],
                        'done_ground': params['done_ground'],
                        'spectral_normalization': params['spectral_normalization'],
                        'dual_reg': params['dual_reg'],
                        'dual_init_lambda': params['dual_init_lambda'],
                        'dual_lam_opt': params['dual_lam_opt'],
                        'dual_slack': params['dual_slack'],
                        'dual_dist': params['dual_dist'],
                        'inner': params['inner'],
                        'algo': params['algo'],
                        'random_eps': params['random_eps'],
                        'noise_eps': params['noise_eps'],
                        'sk_lam_lr': params['sk_lam_lr'],
                        'algo_name': params['algo_name'],
                        'train_start_epoch': params['train_start_epoch'],
                        })
    ddpg_params['info'] = {
        'env_name': params['env_name'],
    }
    policy = DDPG(reuse=reuse, **ddpg_params, use_mpi=use_mpi)
    return policy


def configure_dims(params):
    env = cached_make_env(params['make_env'])
    env.reset()
    obs, _, _, info = env.step(env.action_space.sample())

    if isinstance(obs, dict):
        dims = {
            'o': obs['observation'].shape[0],
            'z': params['num_skills'],
            'u': env.action_space.shape[0],
            'g': obs['desired_goal'].shape[0],
        }
        for key, value in info.items():
            if 'TimeLimit' in key:
                continue
            value = np.array(value)
            if value.ndim == 0:
                value = value.reshape(1)
            dims['info_{}'.format(key)] = value.shape[0]
    else:
        dims = {
            'o': obs.shape[0],
            'z': params['num_skills'],
            'u': env.action_space.shape[0],
            'g': 3,
        }
    return dims
