from __future__ import print_function

import os
import sys
import numpy as np
import copy
import random
import threading
from Queue import Queue
import time
import math
import cPickle as pickle
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
ROOT_DIR = os.path.dirname(BASE_DIR)
sys.path.append(os.path.join(ROOT_DIR, 'kitti'))
sys.path.append(os.path.join(ROOT_DIR, 'utils'))
sys.path.append(os.path.join(ROOT_DIR, 'visualize/mayavi'))
from kitti_object import *
from parameterize import obj_to_proposal_vec
import kitti_util as utils
from data_util import rotate_points_along_y, shift_point_cloud, extract_pc_in_box3d
from data_util import ProposalObject, np_read_lines, find_match_label
from data_conf import type_whitelist, difficulties_whitelist

g_type2onehotclass = {'NonObject': 0, 'Car': 1, 'Pedestrian': 2, 'Cyclist': 3}

class Dataset(object):
    def __init__(self, npoints, kitti_path, split, \
        types=type_whitelist, difficulties=difficulties_whitelist):
        self.npoints = npoints
        self.kitti_path = kitti_path
        self.proposal_dir = os.path.join(kitti_path, 'training', 'proposal')
        #self.batch_size = batch_size
        self.split = split
        self.kitti_dataset = kitti_object(kitti_path, 'training')
        self.frame_ids = self.load_split_ids(split)
        random.shuffle(self.frame_ids)
        # self.frame_ids = self.frame_ids[:1]
        self.num_channel = 6 # xyz intensity is_obj_one_hot
        self.AUG_X = 1

        self.types_list = types
        self.difficulties_list = difficulties

        self.sample_id_counter = -1 # as id for sample
        self.last_sample_id = None
        # preloading
        self.stop = False
        self.data_buffer = Queue(maxsize=128)

    def load_split_ids(self, split):
        with open(os.path.join(self.kitti_path, split + '.txt')) as f:
            return [line.rstrip('\n') for line in f]

    def load(self, save_path, aug=False):
        i = 0
        last_sample_id = None
        while not self.stop:
            frame_id = self.frame_ids[i]
            #print('loading ' + frame_id)
            for x in range(self.AUG_X):
                frame_data = {}
                samples = \
                    self.load_frame_data(frame_id, random_flip=aug, random_rotate=aug, random_shift=aug)
                for s in samples:
                    s['frame_id'] = frame_id
                    self.data_buffer.put(s)
            if len(samples) > 0:
                last_sample_id = samples[-1]['id']
            if i == len(self.frame_ids) - 1:
                self.last_sample_id = last_sample_id
                random.shuffle(self.frame_ids)
            i = (i + 1) % len(self.frame_ids)

    def stop_loading(self):
        self.stop = True
        while not self.data_buffer.empty():
            item = self.data_buffer.get()
            self.data_buffer.task_done()

    def get_next_batch(self, bsize, need_id=False):
        is_last_batch = False

        batch = {
            'ids': [],
            'pointcloud': np.zeros((bsize, self.npoints, self.num_channel), dtype=np.float32),
            'images': np.zeros((bsize, 360, 1200, 3), dtype=np.float32),
            'calib': np.zeros((bsize, 3, 4), dtype=np.float32),
            'label': np.zeros((bsize,), dtype=np.int32),
            'prop_box': np.zeros((bsize, 7), dtype=np.float32),
            'center_x_cls': np.zeros((bsize,), dtype=np.int32),
            'center_z_cls': np.zeros((bsize,), dtype=np.int32),
            'center_x_res': np.zeros((bsize,), dtype=np.float32),
            'center_y_res': np.zeros((bsize,), dtype=np.float32),
            'center_z_res': np.zeros((bsize,), dtype=np.float32),
            'angle_cls': np.zeros((bsize,), dtype=np.int32),
            'size_cls': np.zeros((bsize,), dtype=np.int32),
            'angle_res': np.zeros((bsize,), dtype=np.float32),
            'size_res': np.zeros((bsize, 3), dtype=np.float32),
            'gt_box_of_prop': np.zeros((bsize, 8, 3), dtype=np.float32)
        }
        for i in range(bsize):
            sample = self.data_buffer.get()
            if sample['id'] == self.last_sample_id:
                is_last_batch = True
                self.last_sample_id = None
            batch['ids'].append(sample['frame_id'])
            choice = np.random.choice(sample['pointcloud'].shape[0], self.npoints, replace=True)
            batch['pointcloud'][i,...] = sample['pointcloud'][choice]
            batch['calib'][i,...] = sample['calib']
            batch['images'][i,...] = sample['image']
            batch['label'][i] = sample['class']
            batch['prop_box'][i,...] = sample['proposal_box']
            batch['center_x_cls'][i] = sample['center_cls'][0]
            batch['center_z_cls'][i] = sample['center_cls'][1]
            batch['center_x_res'][i] = sample['center_res'][0]
            batch['center_y_res'][i] = sample['center_res'][1]
            batch['center_z_res'][i] = sample['center_res'][2]
            batch['angle_cls'][i] = sample['angle_cls']
            batch['size_cls'][i] = sample['size_cls']
            batch['angle_res'][i] = sample['angle_res']
            batch['size_res'][i,...] = sample['size_res']
            batch['gt_box_of_prop'][i,...] = sample['gt_box']

        return batch, is_last_batch

    def viz_frame(self, pc_rect, mask, gt_boxes):
        import mayavi.mlab as mlab
        from viz_util import draw_lidar, draw_lidar_simple, draw_gt_boxes3d
        fig = draw_lidar(pc_rect)
        fig = draw_lidar(pc_rect[mask==1], fig=fig, pts_color=(1, 1, 1))
        fig = draw_gt_boxes3d(gt_boxes, fig, draw_text=False, color=(1, 1, 1))
        raw_input()

    def load_proposals(self, data_idx):
        # Generate proposals from labels for now
        objects = self.kitti_dataset.get_label_objects(data_idx)
        objects = filter(lambda obj: obj.type in self.types_list and obj.difficulty in self.difficulties_list, objects)
        proposals = []
        avg_y = 0
        for obj in objects:
            center = obj.t + np.random.normal(0, 0.1, 3)
            ry = obj.ry + np.random.normal(0, np.pi/8, 1)
            l = obj.l + np.random.normal(0, 0.1, 1)[0]
            h = obj.h + np.random.normal(0, 0.1, 1)[0]
            w = obj.w + np.random.normal(0, 0.1, 1)[0]
            proposals.append(ProposalObject(np.array([center[0],center[1],center[2],l, w, h, ry])))
            avg_y += obj.t[1]

        # TODO: negative samples
        return proposals

    def load_frame_data(self, data_idx_str,
        random_flip=False, random_rotate=False, random_shift=False):
        '''load one frame'''
        start = time.time()
        data_idx = int(data_idx_str)
        # print(data_idx_str)
        calib = self.kitti_dataset.get_calibration(data_idx) # 3 by 4 matrix
        objects = self.kitti_dataset.get_label_objects(data_idx)
        image = self.kitti_dataset.get_image(data_idx)
        pc_velo = self.kitti_dataset.get_lidar(data_idx)
        img_height, img_width = image.shape[0:2]
        _, pc_image_coord, img_fov_inds = get_lidar_in_image_fov(pc_velo[:,0:3],
            calib, 0, 0, img_width, img_height, True)
        pc_velo = pc_velo[img_fov_inds, :]
        #print(data_idx_str, pc_velo.shape[0])
        point_set = pc_velo
        pc_rect = np.zeros_like(point_set)
        pc_rect[:,0:3] = calib.project_velo_to_rect(point_set[:,0:3])
        pc_rect[:,3] = point_set[:,3]
        objects = filter(lambda obj: obj.type in self.types_list and obj.difficulty in self.difficulties_list, objects)
        gt_boxes = [] # ground truth boxes
        gt_boxes_xy = []
        for obj in objects:
            _,obj_box_3d = utils.compute_box_3d(obj, calib.P)
            # doesn't skip label with no point here
            gt_boxes.append(obj_box_3d)
            gt_boxes_xy.append(obj_box_3d[:4, [0,2]])
        proposals = self.load_proposals(data_idx)
        positive_samples = []
        negative_samples = []
        show_boxes = []
        # boxes_2d = []
        for prop in proposals:
            b2d,prop_box_3d = utils.compute_box_3d(prop, calib.P)
            prop_box_xy = prop_box_3d[:4, [0,2]]
            gt_idx, gt_iou = find_match_label(prop_box_xy, gt_boxes_xy)
            if gt_iou < 0.55:
                sample = self.get_sample(pc_rect, image, calib, prop)
                if sample:
                    negative_samples.append(sample)
                    # show_boxes.append(prop_box_3d)
            else:
                sample = self.get_sample(pc_rect, image, calib, prop, objects[gt_idx])
                if sample:
                    positive_samples.append(sample)
                    # boxes_2d.append(b2d)
                    show_boxes.append(prop_box_3d)
        #print('positive:', len(positive_samples))
        #print('negative:', len(negative_samples))
        random.shuffle(negative_samples)
        samples = positive_samples + negative_samples[:len(positive_samples)]
        random.shuffle(samples)
        # self.viz_frame(pc_rect, np.zeros((pc_rect.shape[0],)), show_boxes)
        return samples

    def get_sample(self, pc_rect, image, calib, proposal_, label=None):
        # TODO: litmit y
        # expand proposal boxes
        proposal = copy.deepcopy(proposal_)
        # proposal.l += 1
        # proposal.w += 1
        _, box_3d = utils.compute_box_3d(proposal, calib.P)
        _, mask = extract_pc_in_box3d(pc_rect, box_3d)
        if(np.sum(mask) == 0):
            return False

        points = pc_rect[mask,:]
        points_with_feats = np.zeros((points.shape[0], self.num_channel))
        points_with_feats[:,:4] = points # xyz and intensity
        points_with_feats[:,:3] -= proposal.t # normalize
        points_with_feats[:,4:6] = np.array([1, 0]) # one hot

        sample = {}
        self.sample_id_counter += 1
        sample['id'] = self.sample_id_counter
        sample['class'] = 0
        sample['pointcloud'] = points_with_feats
        sample['image'] = cv2.resize(image, (1200, 360))
        sample['calib'] = np.copy(calib.P)
        # scale projection matrix
        sample['calib'][0,:] *= (1200.0 / image.shape[1])
        sample['calib'][1,:] *= (360.0 / image.shape[0])
        sample['proposal_box'] = np.array([proposal.t[0], proposal.t[1], proposal.t[2],
            proposal.ry, proposal.l, proposal.h, proposal.w])
        sample['center_cls'] = np.zeros((2,), dtype=np.int32)
        sample['center_res'] = np.zeros((3,))
        sample['angle_cls'] = 0
        sample['angle_res'] = 0
        sample['size_cls'] = 0
        sample['size_res'] = np.zeros((3,))
        sample['gt_box'] = np.zeros((8,3))
        if label:
            sample['class'] = g_type2onehotclass[label.type]
            obj_vec = obj_to_proposal_vec(label, proposal.t)
            # test
            # sample['proposal_box'] = np.array([label.t[0], label.t[1], label.t[2],
            #     label.ry, label.l, label.h, label.w])

            sample['center_cls'] = obj_vec[0]
            sample['center_res'] = obj_vec[1]
            sample['angle_cls'] = obj_vec[2]
            sample['angle_res'] = obj_vec[3]
            sample['size_cls'] = obj_vec[4]
            sample['size_res'] = obj_vec[5]
            _, gt_box_3d = utils.compute_box_3d(label, calib.P)
            sample['gt_box'] = gt_box_3d
            _, gt_mask = extract_pc_in_box3d(points, gt_box_3d)
            sample['pointcloud'][gt_mask,4:6] = np.array([0, 1]) # one hot
        return sample

if __name__ == '__main__':
    kitti_path = sys.argv[1]
    split = sys.argv[2]
    dataset = Dataset(512, kitti_path, split)
    dataset.load('./train', True)

    sys.path.append('../models')
    from collections import namedtuple
    import tensorflow as tf
    from img_vgg_pyramid import ImgVggPyr
    import projection
    VGG_config = namedtuple('VGG_config', 'vgg_conv1 vgg_conv2 vgg_conv3 vgg_conv4 l2_weight_decay')

    produce_thread = threading.Thread(target=dataset.load, args=('./train',True))
    produce_thread.start()
    i = 0
    total = 0
    while(True):
        batch_data, is_last_batch = dataset.get_next_batch(1, need_id=True)
        # total += np.sum(batch_data[1] == 1)
        # print('foreground points:', np.sum(batch_data[1] == 1))
        print(batch_data['ids'])
        with tf.Session() as sess:
            img_vgg = ImgVggPyr(VGG_config(**{
                'vgg_conv1': [2, 32],
                'vgg_conv2': [2, 64],
                'vgg_conv3': [3, 128],
                'vgg_conv4': [3, 256],
                'l2_weight_decay': 0.0005
            }))
            img_pixel_size = np.asarray([360, 1200])
            img_preprocessed = img_vgg.preprocess_input(batch_data['images'], img_pixel_size)
            box2d_corners, box2d_corners_norm = projection.tf_project_to_image_space(
                batch_data['prop_box'],
                batch_data['calib'], img_pixel_size)

            box2d_corners_norm_reorder = tf.stack([
                tf.gather(box2d_corners_norm, 1, axis=-1),
                tf.gather(box2d_corners_norm, 0, axis=-1),
                tf.gather(box2d_corners_norm, 3, axis=-1),
                tf.gather(box2d_corners_norm, 2, axis=-1),
            ], axis=-1)
            img_rois = tf.image.crop_and_resize(
                img_preprocessed,
                box2d_corners_norm_reorder, # reorder
                tf.range(0, 1),
                [100,100])
            corners, corners_norm = sess.run([box2d_corners,box2d_corners_norm])
            # break
            whole_img = cv2.resize(batch_data['images'][0]/255, (1200,360))
            # corner = corners[0].astype(int)
            corner = (corners_norm[0] * np.array([1200,360,1200,360])).astype(int)
            print('proposal box: ', batch_data['prop_box'][0])
            print('projection matrix: ', batch_data['calib'][0])
            print(corner, corners[0].astype(int))
            cv2.rectangle(whole_img,(corner[0], corner[1]),(corner[2], corner[3]),(55,255,155),2)
            cv2.imshow('img1', whole_img)

            res = sess.run(img_rois)
            # print(res[0]+98)
            cv2.imshow('img', (res[0]+98)/255)
            cv2.waitKey(0)
        # break
        # if i >= 10:
        if is_last_batch:
            break
        i += 1
    dataset.stop_loading()
    print('stop loading')
    produce_thread.join()
    '''
    '''