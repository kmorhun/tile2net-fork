from __future__ import annotations, absolute_import, division
import time

import numpy

"""
Copyright 2020 Nvidia Corporation
Redistribution and use in source and binary forms, with or without
modification, are permitted provided that the following conditions are met:
1. Redistributions of source code must retain the above copyright notice, this
   list of conditions and the following disclaimer.
2. Redistributions in binary form must reproduce the above copyright notice,
   this list of conditions and the following disclaimer in the documentation
   and/or other materials provided with the distribution.
3. Neither the name of the copyright holder nor the names of its contributors
   may be used to endorse or promote products derived from this software
   without specific prior written permission.
THIS SOFTWARE IS PROVIDED BY THE COPYRIGHT HOLDERS AND CONTRIBUTORS "AS IS"
AND ANY EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT LIMITED TO, THE
IMPLIED WARRANTIES OF MERCHANTABILITY AND FITNESS FOR A PARTICULAR PURPOSE
ARE DISCLAIMED. IN NO EVENT SHALL THE COPYRIGHT HOLDER OR CONTRIBUTORS BE
LIABLE FOR ANY DIRECT, INDIRECT, INCIDENTAL, SPECIAL, EXEMPLARY, OR
CONSEQUENTIAL DAMAGES (INCLUDING, BUT NOT LIMITED TO, PROCUREMENT OF
SUBSTITUTE GOODS OR SERVICES; LOSS OF USE, DATA, OR PROFITS; OR BUSINESS
INTERRUPTION) HOWEVER CAUSED AND ON ANY THEORY OF LIABILITY, WHETHER IN
CONTRACT, STRICT LIABILITY, OR TORT (INCLUDING NEGLIGENCE OR OTHERWISE)
ARISING IN ANY WAY OUT OF THE USE OF THIS SOFTWARE, EVEN IF ADVISED OF THE
POSSIBILITY OF SUCH DAMAGE.
"""

from geopandas import GeoDataFrame, GeoSeries
import pandas as pd
import geopandas as gpd

import os
import sys

import argh
import torch
from torch.utils.data import DataLoader
import torch.distributed as dist
from torch.cuda import amp
from runx.logx import logx

import tile2net.tileseg.network.ocrnet
from tile2net.tileseg.config import assert_and_infer_cfg, cfg
from tile2net.tileseg.utils.misc import AverageMeter, prep_experiment
from tile2net.tileseg.utils.misc import ImageDumper, ThreadedDumper
from tile2net.tileseg.utils.trnval_utils import eval_minibatch
from tile2net.tileseg.loss.utils import get_loss
from tile2net.tileseg.loss.optimizer import get_optimizer, restore_opt, restore_net

from tile2net.tileseg import datasets
from tile2net.tileseg import network
from tile2net.tileseg.inference.commandline import commandline
from tile2net.namespace import Namespace
from tile2net.logger import logger

from tile2net.raster.pednet import PedNet
import logging

import numpy as np
import copy


sys.path.append(os.environ.get('SUBMIT_SCRIPTS', '.'))
AutoResume = None

from tile2net.raster.project import Project


@commandline
def inference_(args: Namespace):
    ...
    # sys.stdin
    if args.dump_percent:
        if not os.path.exists(args.result_dir):
            os.mkdir(args.result_dir)
        assert os.path.exists(args.result_dir), 'Result directory does not exist'
        logging.info(f'Inferencing. Segmentation results will be saved to {args.result_dir}')
    else:
        logging.info('Inferencing. Segmentation results will not be saved.')

    # Project.resources.assets.weights.satellite_2021
    weights = Project.resources.assets.weights
    if (
            args.model.snapshot == weights.satellite_2021.path.absolute().__fspath__()
            and not weights.satellite_2021.path.exists()
    ) or (
            args.model.hrnet_checkpoint == weights.hrnetv2_w48_imagenet_pretrained.path.absolute().__fspath__()
            and not weights.hrnetv2_w48_imagenet_pretrained.path.exists()
    ):
        weights.path.mkdir(parents=True, exist_ok=True)
        logging.info(
            "Downloading weights for segmentation, this may take a while..."
        )
        weights.download()
        logging.info("Weights downloaded.")

    args.best_record = {'epoch': -1, 'iter': 0, 'val_loss': 1e10, 'acc': 0,
                        'acc_cls': 0, 'mean_iu': 0, 'fwavacc': 0}

    # Enable CUDNN Benchmarking optimization
    torch.backends.cudnn.benchmark = True
    if args.deterministic:
        torch.backends.cudnn.deterministic = True
        torch.backends.cudnn.benchmark = False

    args.world_size = 1

    # Test Mode run two epochs with a few iterations of training and val
    if args.options.test_mode:
        args.max_epoch = 2

    num_gpus = torch.cuda.device_count()
    if num_gpus > 1:
        # Distributed training setup

        args.world_size = int(os.environ.get('WORLD_SIZE', num_gpus))
        dist.init_process_group(backend='nccl', init_method='env://')
        args.local_rank = dist.get_rank()
        torch.cuda.set_device(args.local_rank)
        args.distributed = True
        args.global_rank = int(os.environ['RANK'])
        print(f'Using distributed training with {args.world_size} GPUs.')
    elif num_gpus == 1:
        # Single GPU setup
        print('Using a single GPU.')
        args.local_rank = 0
        torch.cuda.set_device(args.local_rank)
    else:
        # CPU setup
        print('Using CPU.')
        args.local_rank = -1  # Indicating CPU usage

    # if 'WORLD_SIZE' in os.environ and args.model.apex:
    #     # args.model.apex = int(os.environ['WORLD_SIZE']) > 1
    #     args.world_size = int(os.environ['WORLD_SIZE'])
    #     args.global_rank = int(os.environ['RANK'])

    # if args.model.apex:
    #     print('Global Rank: {} Local Rank: {}'.format(
    #         args.global_rank, args.local_rank))
    #     torch.cuda.set_device(args.local_rank)
    #     torch.distributed.init_process_group(backend='nccl',
    #                                          init_method='env://')

    # def check_termination(epoch):
    #     if AutoResume:
    #         shouldterminate = AutoResume.termination_requested()
    #         if shouldterminate:
    #             if args.global_rank == 0:
    #                 progress = "Progress %d%% (epoch %d of %d)" % (
    #                     (epoch * 100 / args.max_epoch),
    #                     epoch,
    #                     args.max_epoch
    #                 )
    #                 AutoResume.request_resume(
    #                     user_dict={"RESUME_FILE": logx.save_ckpt_fn,
    #                                "TENSORBOARD_DIR": args.result_dir,
    #                                "EPOCH": str(epoch)
    #                                }, message=progress)
    #                 return 1
    #             else:
    #                 return 1
    #     return 0

    def run_inference(args=args, rasterfactory=None):
        """
        Main Function
        """
        print('arst')
        assert args.result_dir is not None, 'need to define result_dir arg'
        logx.initialize(logdir=str(args.result_dir),
                        tensorboard=True, hparams=vars(args),
                        global_rank=args.global_rank)

        # Set up the Arguments, Tensorboard Writer, Dataloader, Loss Fn, Optimizer
        assert_and_infer_cfg(args)
        prep_experiment(args)
        train_loader, val_loader, train_obj = \
            datasets.setup_loaders(args)
        criterion, criterion_val = get_loss(args)

        if args.model.snapshot:
            if 'ASSETS_PATH' in args.model.snapshot:
                args.model.snapshot = args.model.snapshot.replace('ASSETS_PATH', cfg.ASSETS_PATH)
            checkpoint = torch.load(args.model.snapshot,
                                    map_location=torch.device('cpu'))
            args.restore_net = True
            msg = "Loading weights from: checkpoint={}".format(args.model.snapshot)
            logx.msg(msg)

        net = network.get_net(args, criterion)
        optim, scheduler = get_optimizer(args, net)

        net = network.wrap_network_in_dataparallel(args, net)

        if args.restore_optimizer:
            restore_opt(optim, checkpoint)
        if args.restore_net:
            restore_net(net, checkpoint)

        if args.options.init_decoder:
            net.module.init_mods()

        torch.cuda.empty_cache()

        if args.tile2net:
            if rasterfactory:
                city_data = rasterfactory
            else:
                from tile2net.raster.raster import Raster
                # boundary_path = args.boundary_path
                city_info_path = cfg.CITY_INFO_PATH
                # @maryam boundary path is unused in original
                # boundary_path = cfg.MODEL.boundary_path
                city_data = Raster.from_info(city_info_path)

        else:
            city_data = None
        # There are 4 options for evaluation:
        #  --eval val                           just run validation
        #  --eval val --dump_assets             dump all images and assets
        #  --eval folder                        just dump all basic images
        #  --eval folder --dump_assets          dump all images and assets

        if args.model.eval == 'test':
            validate(val_loader, net, criterion=None, optim=None, epoch=0,
                     calc_metrics=False, dump_assets=args.dump_assets,
                     dump_all_images=True, testing=True, grid=city_data,
                     args=args,
                     )
            return 0

        elif args.model.eval == 'folder':
            # Using a folder for evaluation means to not calculate metrics
            validate(val_loader, net, criterion=criterion_val, optim=None, epoch=0,
                     calc_metrics=False, dump_assets=args.dump_assets,
                     dump_all_images=True,
                     args=args,
                     )
            return 0

        elif args.model.eval is not None:
            raise 'unknown eval option {}'.format(args.eval)

    def validate(val_loader, net, criterion, optim, epoch,
                 args,
                 calc_metrics=True,
                 dump_assets=False,
                 dump_all_images=False, testing=None, grid=None,
                 **kwargs
                 ):
        """
        Run validation for one epoch
        :val_loader: data loader for validation
        :net: the network
        :criterion: loss fn
        :optimizer: optimizer
        :epoch: current epoch
        :calc_metrics: calculate validation score
        :dump_assets: dump attention prediction(s) images
        :dump_all_images: dump all images, not just N
        """
        dumper = ImageDumper(val_len=len(val_loader),
                             dump_all_images=dump_all_images,
                             dump_assets=dump_assets, args=args)

        net.eval()
        val_loss = AverageMeter()
        iou_acc = 0
        pred = dict()
        _temp = dict.fromkeys([i for i in range(10)], None)
        for val_idx, data in enumerate(val_loader):
            input_images, labels, img_names, _ = data

            # Run network
            assets, _iou_acc = \
                eval_minibatch(data, net, criterion, val_loss, calc_metrics,
                               args, val_idx)
            # types = {
            #     k: type(v)
            #     for k, v in assets.items()
            # }
            iou_acc += _iou_acc

            input_images, labels, img_names, _ = data

            if testing:
                prediction = assets['predictions'][0]
                values, counts = np.unique(prediction, return_counts=True)
                pred[img_names[0]] = copy.copy(_temp)

                for v in range(len(values)):
                    pred[img_names[0]][values[v]] = counts[v]

                # pred.update({img_names[0]: dict(zip(values, counts))})
                dumper.dump(
                    {'gt_images': labels, 'input_images': input_images, 'img_names': img_names,
                     'assets': assets},
                    val_idx, testing=True, grid=grid)
            else:
                dumper.dump({'gt_images': labels,
                             'input_images': input_images,
                             'img_names': img_names,
                             'assets': assets}, val_idx)

            if val_idx > 5 and args.options.test_mode:
                break

            if val_idx % 20 == 0:
                logx.msg(f'Inference [Iter: {val_idx + 1} / {len(val_loader)}]')

        if testing:
            if grid:
                grid.save_ntw_polygon()
                polys = grid.ntw_poly
                # net = PedNet(polys, grid.project)
                net = PedNet(
                    poly=polys,
                    project=grid.project,
                )
                net.convert_whole_poly2line()

    run_inference()


class Inference:
    def __init__(self, args: Namespace):
        self.args = args
        if args.dump_percent:
            if not os.path.exists(args.result_dir):
                os.mkdir(args.result_dir)
            logger.info(f'Inferencing. Segmentation results will be saved to {args.result_dir}')
        else:
            logger.info('Inferencing. Segmentation results will not be saved.')

        weights = Project.resources.assets.weights
        if (
                args.model.snapshot == weights.satellite_2021.path.absolute().__fspath__()
                and not weights.satellite_2021.path.exists()
        ) or (
                args.model.hrnet_checkpoint == weights.hrnetv2_w48_imagenet_pretrained.path.absolute().__fspath__()
                and not weights.hrnetv2_w48_imagenet_pretrained.path.exists()
        ):
            weights.path.mkdir(parents=True, exist_ok=True)
            logger.info("Downloading weights for segmentation, this may take a while...")
            weights.download()
            logger.info("Weights downloaded.")

        args.best_record = dict(epoch=-1, iter=0, val_loss=1e10, acc=0, acc_cls=0, mean_iu=0, fwavacc=0)

        # Enable CUDNN Benchmarking optimization
        torch.backends.cudnn.benchmark = True
        if args.deterministic:
            torch.backends.cudnn.deterministic = True
            torch.backends.cudnn.benchmark = False

        args.world_size = 1

        # Test Mode run two epochs with a few iterations of training and val
        if args.options.test_mode:
            args.max_epoch = 2

        num_gpus = torch.cuda.device_count()
        if num_gpus > 1:
            # Distributed training setup

            args.world_size = int(os.environ.get('WORLD_SIZE', num_gpus))
            dist.init_process_group(backend='nccl', init_method='env://')
            args.local_rank = dist.get_rank()
            torch.cuda.set_device(args.local_rank)
            args.distributed = True
            args.global_rank = int(os.environ['RANK'])
            print(f'Using distributed training with {args.world_size} GPUs.')
        elif num_gpus == 1:
            # Single GPU setup
            print('Using a single GPU.')
            args.local_rank = 0
            torch.cuda.set_device(args.local_rank)
        else:
            # CPU setup
            print('Using CPU.')
            args.local_rank = -1  # Indicating CPU usage

        assert args.result_dir is not None, 'need to define result_dir arg'

    def inference(self, rasterfactory=None):
        train_loader: DataLoader
        val_loader: DataLoader
        train_obj: datasets.Loader
        net: torch.nn.parallel.DataParallel
        optim: torch.optim.sgd.SGD

        args = self.args

        logx.initialize(
            logdir=str(args.result_dir),
            tensorboard=True, hparams=vars(args),
            global_rank=args.global_rank
        )

        assert_and_infer_cfg(args)
        prep_experiment(args)
        train_loader, val_loader, train_obj = datasets.setup_loaders(args)
        criterion, criterion_val = get_loss(args)
        if args.model.snapshot:
            if 'ASSETS_PATH' in args.model.snapshot:
                args.model.snapshot = args.model.snapshot.replace('ASSETS_PATH', cfg.ASSETS_PATH)
            checkpoint = torch.load(args.model.snapshot, map_location=torch.device('cpu'))
            args.restore_net = True
            msg = "Loading weights from: checkpoint={}".format(args.model.snapshot)
            logx.msg(msg)

        net: tile2net.tileseg.network.ocrnet.MscaleOCR = network.get_net(args, criterion)

        optim, scheduler = get_optimizer(args, net)


        net = network.wrap_network_in_dataparallel(args, net)
        if args.restore_optimizer:
            restore_opt(optim, checkpoint)
        if args.restore_net:
            restore_net(net, checkpoint)
        if args.options.init_decoder:
            net.module.init_mods()
        torch.cuda.empty_cache()

        if args.tile2net:
            if rasterfactory:
                city_data = rasterfactory
            else:
                from tile2net.raster.raster import Raster
                # boundary_path = args.boundary_path
                city_info_path = cfg.CITY_INFO_PATH
                # @maryam boundary path is unused in original
                # boundary_path = cfg.MODEL.boundary_path
                city_data = Raster.from_info(city_info_path)
        else:
            city_data = None

        match args.model.eval:
            case 'test':
                self.validate(
                    val_loader, net, criterion=None, optim=None, epoch=0,
                    calc_metrics=False, dump_assets=args.dump_assets,
                    dump_all_images=True, testing=True, grid=city_data,
                    args=args,
                )
                return 0

            case 'folder':
                # Using a folder for evaluation means to not calculate metrics
                self.validate(
                    val_loader, net, criterion=criterion_val, optim=optim, epoch=0,
                    calc_metrics=False, dump_assets=args.dump_assets,
                    dump_all_images=True,
                    args=args,
                )
                return 0

            case _:
                raise 'unknown eval option {}'.format(args.eval)

    def validate(
            self,
            val_loader: DataLoader,
            net: torch.nn.parallel.DataParallel,
            criterion: tile2net.tileseg.loss.utils.CrossEntropyLoss2d,
            optim: torch.optim.sgd.SGD,
            epoch: int,
            calc_metrics=True,
            dump_assets=False,
            dump_all_images=False,
            testing=None,
            grid=None,
            **kwargs
    ):
        """
        Run validation for one epoch
        :val_loader: data loader for validation
        """
        input_images: torch.Tensor
        labels: torch.Tensor
        img_names: tuple
        prediction: numpy.ndarray
        pred: dict
        values: numpy.ndarray
        args = self.args
        # todo map_feature is how Tile generates poly
        gdfs: list[GeoDataFrame] = []

        dumper = ThreadedDumper(
            val_len=len(val_loader),
            dump_all_images=dump_all_images,
            dump_assets=dump_assets,
            args=args,
        )

        net.eval()
        val_loss = AverageMeter()
        iou_acc = 0
        pred = dict()
        _temp = dict.fromkeys([i for i in range(10)], None)
        for val_idx, data in enumerate(val_loader):
            input_images, labels, img_names, _ = data

            # Run network
            assets, _iou_acc = eval_minibatch(
                data, net, criterion, val_loss, calc_metrics, args, val_idx,
            )

            iou_acc += _iou_acc

            input_images, labels, img_names, _ = data

            dumpdict = dict(
                gt_images=labels,
                input_images=input_images,
                img_names=img_names,
                assets=assets,
            )
            if testing:
                prediction = assets['predictions'][0]
                values, counts = np.unique(prediction, return_counts=True)
                pred[img_names[0]] = copy.copy(_temp)

                for v in range(len(values)):
                    pred[img_names[0]][values[v]] = counts[v]

                dump = dumper.dump(dumpdict, val_idx, testing=True, grid=grid)
            else:
                dump = dumper.dump(dumpdict, val_idx)
            gdfs.extend(dump)

            if (
                    args.options.test_mode
                    and val_idx > 5
            ):
                break

            if val_idx % 20 == 0:
                logx.msg(f'Inference [Iter: {val_idx + 1} / {len(val_loader)}]')

        if testing:
            if grid:
                # todo: for now we concate from a list of all the polygons generated during the session;
                #   eventually we will serialize all the files and then use dask for batching
                if not gdfs:
                    poly_network = gpd.GeoDataFrame()
                    logging.warning(
                        f'No polygons were dumped'
                    )
                else:
                    poly_network = pd.concat(gdfs)
                del gdfs

                grid.save_ntw_polygons(poly_network)
                polys = grid.ntw_poly
                net = PedNet(poly=polys, project=grid.project)
                net.convert_whole_poly2line()

@commandline
def inference(args: Namespace):
    return Inference(args).inference()

if __name__ == '__main__':
    """
    --city_info
    /tmp/tile2net/central_park/tiles/central_park_256_info.json
    --interactive
    --dump_percent 100
    """
    from tile2net import Raster

    argh.dispatch_command(inference)
