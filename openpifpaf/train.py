"""Train a pifpaf network."""

import argparse
import copy
import datetime
import logging
import os
import socket

import torch

from . import datasets, encoder, logger, network, optimize, plugin, show, visualizer
from . import __version__

LOG = logging.getLogger(__name__)


def default_output_file(args):
    base_name = args.basenet
    if not base_name:
        base_name, _, __ = os.path.basename(args.checkpoint).partition('-')

    now = datetime.datetime.now().strftime('%y%m%d-%H%M%S')
    out = 'outputs/{}-{}-{}'.format(base_name, now, args.dataset)
    if args.cocokp_square_edge != 385:
        out += '-edge{}'.format(args.cocokp_square_edge)
    if args.regression_loss != 'laplace':
        out += '-{}'.format(args.regression_loss)
    if args.r_smooth != 0.0:
        out += '-rsmooth{}'.format(args.r_smooth)
    if args.cocokp_orientation_invariant or args.cocokp_extended_scale:
        out += '-'
        if args.cocokp_orientation_invariant:
            out += 'o{:02.0f}'.format(args.cocokp_orientation_invariant * 100.0)
        if args.cocokp_extended_scale:
            out += 's'

    return out + '.pkl'


def cli():
    plugin.register()

    parser = argparse.ArgumentParser(
        prog='python3 -m openpifpaf.train',
        usage='%(prog)s [options]',
        description=__doc__,
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument('--version', action='version',
                        version='OpenPifPaf {version}'.format(version=__version__))
    parser.add_argument('-o', '--output', default=None,
                        help='output file')
    parser.add_argument('--disable-cuda', action='store_true',
                        help='disable CUDA')
    parser.add_argument('--ddp', default=False, action='store_true',
                        help='[experimental] DistributedDataParallel')
    parser.add_argument('--local_rank', default=None, type=int,
                        help='[experimental] for torch.distributed.launch')

    logger.cli(parser)
    network.Factory.cli(parser)
    network.losses.Factory.cli(parser)
    network.Trainer.cli(parser)
    encoder.cli(parser)
    optimize.cli(parser)
    datasets.cli(parser)
    show.cli(parser)
    visualizer.cli(parser)

    args = parser.parse_args()

    logger.configure(args, LOG)
    if args.log_stats:
        logging.getLogger('openpifpaf.stats').setLevel(logging.DEBUG)

    # slurm
    slurm_process_id = os.environ.get('SLURM_PROCID')
    if slurm_process_id is not None:
        LOG.info('found SLURM process id: %s', slurm_process_id)
        args.local_rank = 0
        os.environ['RANK'] = slurm_process_id
        if not os.environ.get('WORLD_SIZE') and os.environ.get('SLURM_NTASKS'):
            os.environ['WORLD_SIZE'] = os.environ.get('SLURM_NTASKS')
        LOG.info('distributed env: master=%s port=%s rank=%s world=%s',
                 os.environ.get('MASTER_ADDR'), os.environ.get('MASTER_PORT'),
                 os.environ.get('RANK'), os.environ.get('WORLD_SIZE'))

    # add args.device
    args.device = torch.device('cpu')
    args.pin_memory = False
    if not args.disable_cuda and torch.cuda.is_available():
        args.device = torch.device('cuda')
        args.pin_memory = True
    LOG.debug('neural network device: %s', args.device)

    # output
    if args.output is None:
        args.output = default_output_file(args)
        os.makedirs('outputs', exist_ok=True)

    network.Factory.configure(args)
    network.losses.Factory.configure(args)
    network.Trainer.configure(args)
    encoder.configure(args)
    datasets.configure(args)
    show.configure(args)
    visualizer.configure(args)

    return args


def main():
    args = cli()

    datamodule = datasets.factory(args.dataset)

    net_cpu, start_epoch = network.Factory().factory(head_metas=datamodule.head_metas)
    loss = network.losses.Factory().factory(net_cpu.head_nets)

    checkpoint_shell = None
    if not args.disable_cuda and torch.cuda.device_count() > 1 and not args.ddp:
        LOG.info('Multiple GPUs with DataParallel: %d', torch.cuda.device_count())
        checkpoint_shell = copy.deepcopy(net_cpu)
        net = torch.nn.DataParallel(net_cpu.to(device=args.device))
        loss = loss.to(device=args.device)
    elif not args.disable_cuda and torch.cuda.device_count() == 1 and not args.ddp:
        LOG.info('Single GPU training')
        checkpoint_shell = copy.deepcopy(net_cpu)
        net = net_cpu.to(device=args.device)
        loss = loss.to(device=args.device)
    elif not args.disable_cuda and torch.cuda.device_count() > 0:
        LOG.info('Multiple GPUs with DistributedDataParallel')
        assert not list(loss.parameters())
        assert torch.cuda.device_count() > 0
        checkpoint_shell = copy.deepcopy(net_cpu)
        torch.cuda.set_device(args.local_rank)
        torch.distributed.init_process_group(backend='nccl', init_method='env://')
        LOG.info('DDP: rank %d, world %d',
                 torch.distributed.get_rank(), torch.distributed.get_world_size())
        net_cpu = torch.nn.SyncBatchNorm.convert_sync_batchnorm(net_cpu)
        net = torch.nn.parallel.DistributedDataParallel(net_cpu.to(device=args.device),
                                                        device_ids=[args.local_rank],
                                                        output_device=args.local_rank)
        loss = loss.to(device=args.device)
    else:
        net = net_cpu

    logger.train_configure(args)
    train_loader = network.Trainer.ensure_distributed_sampler(datamodule.train_loader(), 0)
    val_loader = network.Trainer.ensure_distributed_sampler(datamodule.val_loader(), 0)

    optimizer = optimize.factory_optimizer(
        args, list(net.parameters()) + list(loss.parameters()))
    lr_scheduler = optimize.factory_lrscheduler(args, optimizer, len(train_loader))
    trainer = network.Trainer(
        net, loss, optimizer, args.output,
        checkpoint_shell=checkpoint_shell,
        lr_scheduler=lr_scheduler,
        device=args.device,
        model_meta_data={
            'args': vars(args),
            'version': __version__,
            'plugin_versions': plugin.versions(),
            'hostname': socket.gethostname(),
        },
    )
    trainer.loop(train_loader, val_loader, start_epoch=start_epoch)


if __name__ == '__main__':
    main()
