import argparse
import os
import time
from collections import OrderedDict
from copy import deepcopy

import medmnist
import numpy as np
import torch
import torch.nn as nn
import torch.optim as optim
import torch.utils.data as data
import torchvision.transforms as transforms
from acsconv.converters import ACSConverter, Conv2_5dConverter, Conv3dConverter
from medmnist import INFO, Evaluator
from models import ResNet18, ResNet50, VisionTransformer3D
from tensorboardX import SummaryWriter
from tqdm import trange
from utils import Transform3D, model_to_syncbn


def main(data_flag, output_root, num_epochs, gpu_ids, batch_size, size, conv, pretrained_3d, download, model_flag, as_rgb, shape_transform, model_path, run):
# Learning rate and scheduoler setup
    lr = 0.001 if model_flag not in ['vit3d'] else 0.0001
    gamma=0.1
    milestones = [0.5 * num_epochs, 0.75 * num_epochs]
# Dataset Information 
    info = INFO[data_flag]
    task = info['task']
    n_channels = 3 if as_rgb else info['n_channels']
    n_classes = len(info['label'])

    DataClass = getattr(medmnist, info['python_class'])
    #GPU setup
    #this parses the gpu ids and set the environment variables for CUDA devices 
    str_ids = gpu_ids.split(',')
    gpu_ids = []
    for str_id in str_ids:
        id = int(str_id)
        if id >= 0:
            gpu_ids.append(id)
    if len(gpu_ids) > 0:
        os.environ["CUDA_VISIBLE_DEVICES"]=str(gpu_ids[0])

    device = torch.device('cuda:{}'.format(gpu_ids[0])) if gpu_ids else torch.device('cpu') 
    
        
    output_root = os.path.join(output_root, data_flag, time.strftime("%y%m%d_%H%M%S"))
    if not os.path.exists(output_root):
        os.makedirs(output_root)

    print('==> Preparing data...')

    train_transform = Transform3D(mul='random') if shape_transform else Transform3D()
    eval_transform = Transform3D(mul='0.5') if shape_transform else Transform3D()
     
    train_dataset = DataClass(split='train', transform=train_transform, download=download, as_rgb=as_rgb, size=size)
    train_dataset_at_eval = DataClass(split='train', transform=eval_transform, download=download, as_rgb=as_rgb, size=size)
    val_dataset = DataClass(split='val', transform=eval_transform, download=download, as_rgb=as_rgb, size=size)
    test_dataset = DataClass(split='test', transform=eval_transform, download=download, as_rgb=as_rgb, size=size)

    #Data Loaders
    train_loader = data.DataLoader(dataset=train_dataset,
                                batch_size=batch_size,
                                shuffle=True)
    train_loader_at_eval = data.DataLoader(dataset=train_dataset_at_eval,
                                batch_size=batch_size,
                                shuffle=False)
    val_loader = data.DataLoader(dataset=val_dataset,
                                batch_size=batch_size,
                                shuffle=False)
    test_loader = data.DataLoader(dataset=test_dataset,
                                batch_size=batch_size,
                                shuffle=False)
    #Model Initialization
    print('==> Building and training model...')

    if model_flag == 'resnet18':
        model = ResNet18(in_channels=n_channels, num_classes=n_classes)
    elif model_flag == 'resnet50':
        model = ResNet50(in_channels=n_channels, num_classes=n_classes)
    elif model_flag == 'vit3d':
        model = VisionTransformer3D(num_classes=n_classes)
    else:
        raise NotImplementedError
    #convolution conversion - this determines the type of convolution to be used also uses model_to_syncbn to apply sunchronized batch normalization 
    #Synchronized Batch Normalization (SyncBN) is a type of batch normalization used for multi-GPU training. Standard batch normalization only normalizes the data within each device (GPU). SyncBN normalizes the input within the whole mini-batch.
    if conv=='ACSConv':
        model = model_to_syncbn(ACSConverter(model))
    if conv=='Conv2_5d':
        model = model_to_syncbn(Conv2_5dConverter(model))
    if conv=='Conv3d':
        if pretrained_3d == 'i3d':
            model = model_to_syncbn(Conv3dConverter(model, i3d_repeat_axis=-3))
        else:
            model = model_to_syncbn(Conv3dConverter(model, i3d_repeat_axis=None))
    
    model = model.to(device)
 #Evaluators
    train_evaluator = medmnist.Evaluator(data_flag, 'train', size=size)
    val_evaluator = medmnist.Evaluator(data_flag, 'val', size=size)
    test_evaluator = medmnist.Evaluator(data_flag, 'test', size=size)
# loss function
    criterion = nn.CrossEntropyLoss()
# model loading
    if model_path is not None:
        model.load_state_dict(torch.load(model_path, map_location=device)['net'], strict=True)
         #that was loading pre-trained model which is if a model path is prvioded, the models state directory is loaded 
        train_metrics = test(model, train_evaluator, train_loader_at_eval, criterion, device, run, output_root)
        val_metrics = test(model, val_evaluator, val_loader, criterion, device, run, output_root)
        test_metrics = test(model, test_evaluator, test_loader, criterion, device, run, output_root)

        print('train  auc: %.5f  acc: %.5f\n' % (train_metrics[1], train_metrics[2]) + \
              'val  auc: %.5f  acc: %.5f\n' % (val_metrics[1], val_metrics[2]) + \
              'test  auc: %.5f  acc: %.5f\n' % (test_metrics[1], test_metrics[2]))

    if num_epochs == 0:
        return

#optimizer and scheduler 
# the scheduler lowers the learning rate by the gamma at the specified milestone indicated above
    optimizer = torch.optim.Adam(model.parameters(), lr=lr)
    scheduler = torch.optim.lr_scheduler.MultiStepLR(optimizer, milestones=milestones, gamma=gamma)
     #logging setup 
    # this sets up log keys for train,val and test metrics also initializes a tensorboard writer for logging metrics
    logs = ['loss', 'auc', 'acc']
    train_logs = ['train_'+log for log in logs]
    val_logs = ['val_'+log for log in logs]
    test_logs = ['test_'+log for log in logs]
    log_dict = OrderedDict.fromkeys(train_logs+val_logs+test_logs, 0)
    
    writer = SummaryWriter(log_dir=os.path.join(output_root, 'Tensorboard_Results'))
#Training loop
#this is the training loop, it iterates over the num of epochs  
    best_auc = 0
    best_epoch = 0
    best_model = deepcopy(model)

    global iteration
    iteration = 0

    for epoch in trange(num_epochs):
         #training calls 'train' functiom for a trainin epoch
        #evaluation evaluates teh model on training validation and test sets 
        train_loss = train(model, train_loader, criterion, optimizer, device, writer)
        
        train_metrics = test(model, train_evaluator, train_loader_at_eval, criterion, device, run)
        val_metrics = test(model, val_evaluator, val_loader, criterion, device, run)
        test_metrics = test(model, test_evaluator, test_loader, criterion, device, run)
        #scheduler adjusts the learning rate according to how teh scheduler was specififed 
        scheduler.step()
        #this loggis is what logss the metrics to tensorboard 
        for i, key in enumerate(train_logs):
            log_dict[key] = train_metrics[i]
        for i, key in enumerate(val_logs):
            log_dict[key] = val_metrics[i]
        for i, key in enumerate(test_logs):
            log_dict[key] = test_metrics[i]

        for key, value in log_dict.items():
            writer.add_scalar(key, value, epoch)
        #best model tracks the best model based on validation AUC and saves it in state
        cur_auc = val_metrics[1]
        if cur_auc > best_auc:
            best_epoch = epoch
            best_auc = cur_auc
            best_model = deepcopy(model)

            print('cur_best_auc:', best_auc)
            print('cur_best_epoch', best_epoch)

    state = {
        'net': model.state_dict(),
    }

    path = os.path.join(output_root, 'best_model.pth')
    torch.save(state, path)
    # final evaluation and logging 
    #this evaluates the best model we saved during training (checkpoints?) and evaluates the model
    train_metrics = test(best_model, train_evaluator, train_loader_at_eval, criterion, device, run, output_root)
    val_metrics = test(best_model, val_evaluator, val_loader, criterion, device, run, output_root)
    test_metrics = test(best_model, test_evaluator, test_loader, criterion, device, run, output_root)

    train_log = 'train  auc: %.5f  acc: %.5f\n' % (train_metrics[1], train_metrics[2])
    val_log = 'val  auc: %.5f  acc: %.5f\n' % (val_metrics[1], val_metrics[2])
    test_log = 'test  auc: %.5f  acc: %.5f\n' % (test_metrics[1], test_metrics[2])

    log = '%s\n' % (data_flag) + train_log + val_log + test_log + '\n'
    print(log)
     #loggs the final results and saves them on a text file
    with open(os.path.join(output_root, '%s_log.txt' % (data_flag)), 'a') as f:
        f.write(log)        
            
    writer.close()
#training function 
#training loop iterates over the training data
#forward pass computes the model ouputs 
#loss calculation computes the loss function 
#backward bass backpropagates teh loss
# optimizer step updates the model parameters
# logging logs te loss to tensorboard 

def train(model, train_loader, criterion, optimizer, device, writer):
    total_loss = []
    global iteration

    model.train()
    for batch_idx, (inputs, targets) in enumerate(train_loader):
        optimizer.zero_grad()
        outputs = model(inputs.to(device))

        targets = torch.squeeze(targets, 1).long().to(device)
        loss = criterion(outputs, targets)

        total_loss.append(loss.item())
        writer.add_scalar('train_loss_logs', loss.item(), iteration)
        iteration += 1

        loss.backward()
        optimizer.step()
    
    epoch_loss = sum(total_loss)/len(total_loss)
    return epoch_loss

#Test function 
# testing loop iterates over the evaluation data without gradient computation
# forward pass, loss calc and metric calculations (AUC and ACC) then logging the mtrics 
def test(model, evaluator, data_loader, criterion, device, run, save_folder=None):

    model.eval()

    total_loss = []
    y_score = torch.tensor([]).to(device)

    with torch.no_grad():
        for batch_idx, (inputs, targets) in enumerate(data_loader):
            outputs = model(inputs.to(device))
        
            targets = torch.squeeze(targets, 1).long().to(device)
            loss = criterion(outputs, targets)
            m = nn.Softmax(dim=1)
            outputs = m(outputs).to(device)
            targets = targets.float().resize_(len(targets), 1)

            total_loss.append(loss.item())

            y_score = torch.cat((y_score, outputs), 0)

        y_score = y_score.detach().cpu().numpy()
        auc, acc = evaluator.evaluate(y_score, save_folder, run)

        test_loss = sum(total_loss) / len(total_loss)

        return [test_loss, auc, acc]

#argument passing: parser.add_argument defines all the Command line arguments that teh script can accept 
if __name__ == '__main__':
    parser = argparse.ArgumentParser(
        description='RUN Baseline model of MedMNIST3D')

    parser.add_argument('--data_flag',
                        default='organmnist3d',
                        type=str)
    parser.add_argument('--output_root',
                        default='./output',
                        help='output root, where to save models',
                        type=str)
    parser.add_argument('--num_epochs',
                        default=100,
                        help='num of epochs of training, the script would only test model if set num_epochs to 0',
                        type=int)
    parser.add_argument('--size',
                        default=28,
                        help='the image size of the dataset, 28 or 64, default=28',
                        type=int)
    parser.add_argument('--gpu_ids',
                        default='0',
                        type=str)
    parser.add_argument('--batch_size',
                        default=32,
                        type=int)
    parser.add_argument('--conv',
                        default='ACSConv',
                        help='choose converter from Conv2_5d, Conv3d, ACSConv',
                        type=str)
    parser.add_argument('--pretrained_3d',
                        default='i3d',
                        type=str)
    parser.add_argument('--download',
                        action="store_true")
    parser.add_argument('--as_rgb',
                        help='to copy channels, tranform shape 1x28x28x28 to 3x28x28x28',
                        action="store_true")
    parser.add_argument('--shape_transform',
                        help='for shape dataset, whether multiply 0.5 at eval',
                        action="store_true")
    parser.add_argument('--model_path',
                        default=None,
                        help='root of the pretrained model to test',
                        type=str)
    parser.add_argument('--model_flag',
                        default='resnet18',
                        help='choose backbone, resnet18/resnet50/vit3d',
                        type=str)
    parser.add_argument('--run',
                        default='model1',
                        help='to name a standard evaluation csv file, named as {flag}_{split}_[AUC]{auc:.3f}_[ACC]{acc:.3f}@{run}.csv',
                        type=str)


    args = parser.parse_args()
    data_flag = args.data_flag
    output_root = args.output_root
    num_epochs = args.num_epochs
    size = args.size
    gpu_ids = args.gpu_ids
    batch_size = args.batch_size
    conv = args.conv
    pretrained_3d = args.pretrained_3d
    download = args.download
    model_flag = args.model_flag
    as_rgb = args.as_rgb
    model_path = args.model_path
    shape_transform = args.shape_transform
    run = args.run

    main(data_flag, output_root, num_epochs, gpu_ids, batch_size, size, conv, pretrained_3d, download, model_flag, as_rgb, shape_transform, model_path, run)
