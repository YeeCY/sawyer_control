import numpy as np
import rospy
import gym
from gym.spaces import Box
from sawyer_control.pd_controllers.joint_angle_pd_controller import AnglePDController
from sawyer_control.core.serializable import Serializable
from sawyer_control.core.multitask_env import MultitaskEnv
from sawyer_control.configs.config import config_dict as config
from sawyer_control.srv import observation
from sawyer_control.srv import getRobotPoseAndJacobian
from sawyer_control.srv import ik
from sawyer_control.srv import angle_action
from sawyer_control.msg import actions
import abc
from sawyer_control.envs.sawyer_env_base import SawyerEnvBase

class SawyerEnvBaseImage(SawyerEnvBase):
    def __init__(
            self,
            img_length=84,
            num_channels=3,
            **kwargs
    ):
        Serializable.quick_init(self, locals())
        super().__init__(**kwargs)
        self.img_length=img_length
        self.num_channels=num_channels
        #todo: set observation space correctly

    def step(self, action):
        self._act(action)
        observation = self._get_obs()
        reward = self.compute_reward(action, self.convert_ob_to_goal(observation), self._state_goal)
        info = self._get_info()
        done = False
        return observation, reward, done, info
    
    def _get_obs(self):
        angles, velocities, endpoint_pose = self.request_image()
        obs = np.hstack((
            self._wrap_angles(angles),
            velocities,
            endpoint_pose,
        ))
        return obs

    @abc.abstractmethod
    def compute_rewards(self, actions, obs, goals):
        pass
    
    def _get_info(self):
        return dict()

    def _safe_move_to_neutral(self):
        for i in range(self.config.RESET_LENGTH):
            cur_pos, cur_vel, _ = self.request_observation()
            torques = self.AnglePDController._compute_pd_forces(cur_pos, cur_vel)
            self._torque_act(torques)
            if self._reset_complete():
                break

    def _reset_complete(self):
        close_to_desired_reset_pos = self._check_reset_angles_within_threshold()
        _, velocities, _ = self.request_observation()
        velocities = np.abs(np.array(velocities))
        VELOCITY_THRESHOLD = .002 * np.ones(7)
        no_velocity = (velocities < VELOCITY_THRESHOLD).all()
        return close_to_desired_reset_pos and no_velocity
    
    def _check_reset_angles_within_threshold(self):
        desired_neutral = self.AnglePDController._des_angles
        desired_neutral = np.array([desired_neutral[joint] for joint in self.config.JOINT_NAMES])
        actual_neutral = (self._get_joint_angles())
        errors = self.compute_angle_difference(desired_neutral, actual_neutral)
        is_within_threshold = (errors < self.config.RESET_ERROR_THRESHOLD).all()
        return is_within_threshold

    def _reset_robot(self):
        self.in_reset = True
        self._safe_move_to_neutral()
        self.in_reset = False

    def reset(self):
        self._reset_robot()
        self._state_goal = self.sample_goal()
        return self._get_obs()

    def get_latest_pose_jacobian_dict(self):
        self.pose_jacobian_dict = self._get_robot_pose_jacobian_client()

    def _get_robot_pose_jacobian_client(self):
        rospy.wait_for_service('get_robot_pose_jacobian')
        try:
            get_robot_pose_jacobian = rospy.ServiceProxy('get_robot_pose_jacobian', getRobotPoseAndJacobian,
                                                         persistent=True)
            resp = get_robot_pose_jacobian('right')
            pose_jac_dict = self._unpack_pose_jacobian_dict(resp.poses, resp.jacobians)
            return pose_jac_dict
        except rospy.ServiceException as e:
            print(e)

    def _unpack_pose_jacobian_dict(self, poses, jacobians):
        pose_jacobian_dict = {}
        pose_counter = 0
        jac_counter = 0
        poses = np.array(poses)
        jacobians = np.array(jacobians)
        for link in self.config.LINK_NAMES:
            pose = poses[pose_counter:pose_counter + 3]
            jacobian = []
            for i in range(jac_counter, jac_counter+21, 7):
                jacobian.append(jacobians[i:i+7])
            jacobian = np.array(jacobian)
            pose_counter += 3
            jac_counter += 21
            pose_jacobian_dict[link] = [pose, jacobian]
        return pose_jacobian_dict

    def _get_positions_from_pose_jacobian_dict(self):
        poses = []
        for joint in self.pose_jacobian_dict.keys():
            poses.append(self.pose_jacobian_dict[joint][0])
        return np.array(poses)

    def get_pose_jacobian_dict_of_joints_not_in_box(self, safety_box):
        joint_dict = self.pose_jacobian_dict.copy()
        keys_to_remove = []
        for joint in joint_dict.keys():
            if self._pose_in_box(joint_dict[joint][0], safety_box):
                keys_to_remove.append(joint)
        for key in keys_to_remove:
            del joint_dict[key]
        return joint_dict

    def _pose_in_box(self, pose, safety_box):
        within_box = safety_box.contains(pose)
        return within_box

    def _get_adjustment_forces_per_joint_dict(self, joint_dict, safety_box):
        forces_dict = {}
        for joint in joint_dict:
            force = self._get_adjustment_force_from_pose(joint_dict[joint][0], safety_box)
            forces_dict[joint] = force
        return forces_dict

    def _get_adjustment_force_from_pose(self, pose, safety_box):
        x, y, z = 0, 0, 0

        curr_x = pose[0]
        curr_y = pose[1]
        curr_z = pose[2]

        if curr_x > safety_box.high[0]:
            x = -1 * np.exp(np.abs(curr_x - safety_box.high[0]) * self.config.SAFETY_FORCE_TEMPERATURE) * self.config.SAFETY_FORCE_MAGNITUDE
        elif curr_x < safety_box.low[0]:
            x = np.exp(np.abs(curr_x - safety_box.low[0]) * self.config.SAFETY_FORCE_TEMPERATURE) * self.config.SAFETY_FORCE_MAGNITUDE

        if curr_y > safety_box.high[1]:
            y = -1 * np.exp(np.abs(curr_y - safety_box.high[1]) * self.config.SAFETY_FORCE_TEMPERATURE) * self.config.SAFETY_FORCE_MAGNITUDE
        elif curr_y < safety_box.low[1]:
            y = np.exp(np.abs(curr_y - safety_box.low[1]) * self.config.SAFETY_FORCE_TEMPERATURE) * self.config.SAFETY_FORCE_MAGNITUDE

        if curr_z > safety_box.high[2]:
            z = -1 * np.exp(np.abs(curr_z - safety_box.high[2]) * self.config.SAFETY_FORCE_TEMPERATURE) * self.config.SAFETY_FORCE_MAGNITUDE
        elif curr_z < safety_box.low[2]:
            z = np.exp(np.abs(curr_z - safety_box.high[2]) * self.config.SAFETY_FORCE_TEMPERATURE) * self.config.SAFETY_FORCE_MAGNITUDE
        return np.array([x, y, z])

    def _compute_joint_distance_outside_box(self, pose, safety_box):
        curr_x = pose[0]
        curr_y = pose[1]
        curr_z = pose[2]
        if(self._pose_in_box(pose)):
            x, y, z = 0, 0, 0
        else:
            x, y, z = 0, 0, 0
            if curr_x > safety_box.high[0]:
                x = np.abs(curr_x - safety_box.high[0])
            elif curr_x < safety_box.low[0]:
                x = np.abs(curr_x - safety_box.low[0])
            if curr_y > safety_box.high[1]:
                y = np.abs(curr_y - safety_box.high[1])
            elif curr_y < safety_box.low[1]:
                y = np.abs(curr_y - safety_box.low[1])
            if curr_z > safety_box.high[2]:
                z = np.abs(curr_z - safety_box.high[2])
            elif curr_z < safety_box.low[2]:
                z = np.abs(curr_z - safety_box.low[2])
        return np.linalg.norm([x, y, z])

    @abc.abstractmethod
    def get_diagnostics(self, paths, prefix=''):
        pass

    def _set_action_space(self):
        if self.action_mode == 'position':
            self.action_space = Box(
                self.config.POSITION_CONTROL_LOW,
                self.config.POSITION_CONTROL_HIGH,
            )
        else:
            self.action_space = Box(
                self.config.JOINT_TORQUE_LOW,
                self.config.JOINT_TORQUE_HIGH
            )

    def _set_observation_space(self):
        lows = np.hstack((
            self.config.JOINT_VALUE_LOW['position'],
            self.config.JOINT_VALUE_LOW['velocity'],
            self.config.END_EFFECTOR_VALUE_LOW['position'],
            self.config.END_EFFECTOR_VALUE_LOW['angle'],
        ))
        highs = np.hstack((
            self.config.JOINT_VALUE_HIGH['position'],
            self.config.JOINT_VALUE_HIGH['velocity'],
            self.config.END_EFFECTOR_VALUE_HIGH['position'],
            self.config.END_EFFECTOR_VALUE_HIGH['angle'],
        ))
        self.observation_space = Box(
            lows,
            highs,
        )
            
    """ 
    ROS Functions 
    """

    def init_rospy(self, update_hz):
        rospy.init_node('sawyer_env', anonymous=True)
        self.action_publisher = rospy.Publisher('actions_publisher', actions, queue_size=10)
        self.rate = rospy.Rate(update_hz)

    def send_action(self, action):
        self.action_publisher.publish(action)

    def send_angle_action(self, action):
        self.request_angle_action(action)

    def request_observation(self):
        rospy.wait_for_service('observations')
        try:
            request = rospy.ServiceProxy('observations', observation, persistent=True)
            obs = request()
            return (
                    np.array(obs.angles),
                    np.array(obs.velocities),
                    np.array(obs.endpoint_pose)
            )
        except rospy.ServiceException as e:
            print(e)

    def request_angle_action(self, angles):
        rospy.wait_for_service('angle_action')
        try:
            execute_action = rospy.ServiceProxy('angle_action', angle_action, persistent=True)
            execute_action(angles)
            return None
        except rospy.ServiceException as e:
            print(e)


    def request_ik_angles(self, ee_pos, joint_angles):
        rospy.wait_for_service('ik')
        try:
            get_joint_angles = rospy.ServiceProxy('ik', ik, persistent=True)
            resp = get_joint_angles(ee_pos, joint_angles)

            return (
                resp.joint_angles
            )
        except rospy.ServiceException as e:
            print(e)

    """
    Multitask functions
    """

    @property
    def goal_dim(self):
        return 3

    def get_goal(self):
        return self._state_goal

    def set_goal(self, goal):
        self._state_goal = goal

    def sample_goals(self, batch_size):
        if self.fix_goal:
            goals = np.repeat(
                self._state_goal.copy()[None],
                batch_size,
                0
            )
        else:
            goals = np.random.uniform(
                self.goal_space.low,
                self.goal_space.high,
                size=(batch_size, self.goal_space.low.size),
            )
        return goals

    @abc.abstractmethod
    def set_to_goal(self, goal):
        pass

#Temporary functions:
#TODO: DELETE THESE ONCE WE SWITCH TO MULTIWORLD
    def sample_goal_for_rollout(self):
        return self.sample_goal()

    def compute_her_reward_np(self, ob, action, next_ob, goal, infos):
        return self.compute_reward(action, self.convert_ob_to_goal(next_ob), goal)