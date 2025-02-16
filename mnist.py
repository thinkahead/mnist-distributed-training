"""
Code to train MNIST using distributed training
"""

# %% Modules

import os
import time
import torch
import torch.nn as nn
import torch.nn.functional as F
import torch.optim as optim
import torch.distributed as dist

from torchvision import transforms
from torchvision import datasets
from filelock import FileLock

# %% Model definition

class MNISTClassifier(nn.Module):
    def __init__(self):
        super(MNISTClassifier, self).__init__()
        self.conv1 = nn.Conv2d(1, 10, kernel_size = 5)
        self.conv2 = nn.Conv2d(10, 20, kernel_size = 5)
        self.conv2_drop = nn.Dropout2d()
        self.fc1 = nn.Linear(320, 50)
        self.fc2 = nn.Linear(50, 10)

    def forward(self, x):
        x = F.relu(F.max_pool2d(self.conv1(x), 2))
        x = F.relu(F.max_pool2d(self.conv2_drop(self.conv2(x)), 2))
        x = x.view(-1, 320)
        x = F.relu(self.fc1(x))
        x = F.dropout(x, training = self.training)
        x = self.fc2(x)
        return F.log_softmax(x, -1)

# %% Training parameters

num_epochs = 10
batch_size = { "train": 64, "test": 1000 }
learning_rate = 0.01
momentum = 0.5
log_interval = 10
seed = 0

# %% Main program

if __name__ == "__main__":
        
    print("Get parallel run data")
    # Get parallel run data
    rank_id = int(os.environ["RANK"])
    num_ranks = int(os.environ["WORLD_SIZE"])
    master_addr = "mnist-training-master-0" # os.environ["MASTER_ADDR"]
    print(os.environ["MASTER_ADDR"],"does not work. Instead using master_addr")
    master_port = os.environ["MASTER_PORT"]
    print("rank_id",rank_id,"num_ranks",num_ranks,"master_addr",master_addr,"master_port",master_port)
    torch.manual_seed(rank_id)
    
    print("Choosing backend")
    # Choose backend to be used
    if torch.cuda.is_available():
        print("nccl cuda")
        # dist.init_process_group(backend="nccl",init_method='env://')
        dist.init_process_group(backend="nccl",init_method='tcp://'+master_addr+':'+str(master_port),rank=rank_id, world_size=num_ranks)
        device = torch.device("cuda")
    else:
        print("gloo cpu")
        # dist.init_process_group(backend="gloo",init_method='env://')
        dist.init_process_group(backend="gloo",init_method='tcp://'+master_addr+':'+str(master_port),rank=rank_id, world_size=num_ranks)
        device = torch.device("cpu")
    
    # Data loaders
    scratch_dir = "/data/.cache/pytorch"

    transform = transforms.Compose([transforms.ToTensor(), transforms.Normalize((0.1307), (0.3081))])

    dataset = {}
    with FileLock("/data/pytorch.lock"):
        dataset["train"] = datasets.MNIST(scratch_dir, train = True, download = True, transform = transform)
        dataset["test"] = datasets.MNIST(scratch_dir, train = False, download = True, transform = transform)

    total_samples = { "train": len(dataset["train"]), "test": len(dataset["test"]) }

    sampler = {}
    sampler["train"] = torch.utils.data.DistributedSampler(dataset["train"], num_replicas = num_ranks, rank = rank_id, shuffle = True, drop_last = False)
    sampler["test"] = torch.utils.data.DistributedSampler(dataset["test"], num_replicas = num_ranks, rank = rank_id, shuffle = True, drop_last = False)

    dataloader = {}
    dataloader["train"] = torch.utils.data.DataLoader(dataset["train"], batch_size = batch_size["train"], shuffle = False, sampler = sampler["train"])
    dataloader["test"] = torch.utils.data.DataLoader(dataset["test"], batch_size = batch_size["test"], shuffle = False, sampler = sampler["test"])

    shard_size = { "train": 0, "test": 0 }

    for batch_id, (features, labels) in enumerate(dataloader["train"]):
        shard_size["train"] += len(features)

    for batch_id, (features, labels) in enumerate(dataloader["test"]):
        shard_size["test"] += len(features)

    # Get model and optimizer
    model = MNISTClassifier().to(device)
    model = torch.nn.parallel.DistributedDataParallel(model)

    optimizer = optim.SGD(model.parameters(), lr = learning_rate, momentum = momentum)

    # Utility functions
    def train(epoch_id, logging = True):
        model.train()
        dataloader["train"].sampler.set_epoch(epoch_id)
        num_samples = 0

        for batch_id, (features, labels) in enumerate(dataloader["train"]):
            features, labels = features.to(device), labels.to(device)

            optimizer.zero_grad()
            output = model(features)
            loss = F.nll_loss(output, labels)
            loss.backward()
            optimizer.step()

            num_samples += len(features)

            if num_samples % log_interval * batch_size["train"] == 0 :
                percentage = 100.0 * num_samples / shard_size["train"]

                if logging:
                    print("Train Epoch (%3d): [%6d/%6d] %%%6.2f Loss: %f" % (epoch_id, num_samples, shard_size["train"], percentage, loss.item()))

                losses["train"].append(loss.item())
                counter["train"].append(epoch_id * total_samples["train"] + num_samples)

        losses["train"].append(loss.item())
        counter["train"].append(epoch_id * total_samples["train"] + num_samples)

    def test(epoch_id, logging = True):
        model.eval()
        dataloader["test"].sampler.set_epoch(epoch_id)

        num_samples = 0
        total_loss = 0.0
        num_correct = 0

        with torch.no_grad():
            for batch_id, (features, labels) in enumerate(dataloader["test"]):
                features, labels = features.to(device), labels.to(device)

                output = model(features)
                total_loss += F.nll_loss(output, labels, size_average = False).item()
                prediction = output.data.max(1, keepdim = True)[1]
                num_correct += prediction.eq(labels.data.view_as(prediction)).sum()
                num_samples += len(features)

            losses["test"].append(total_loss / shard_size["test"])
            counter["test"].append(epoch_id)

            if logging:
                print("Test Epoch (%d): Avg. Loss = %f, Acc. = %d/%d (%%%6.2f)" % (epoch_id, losses["test"][-1], num_correct.item(), num_samples, 100.0 * num_correct.item() / num_samples))

    # Train model
    losses = { "train": [], "test": [] }
    counter = { "train": [], "test": [] }

    t_start = time.time()

    test(0)

    for epoch_id in range(num_epochs):
        train(epoch_id, logging = True)
        test(epoch_id + 1)

    t_end = time.time()

    print("Total training time with '%d' ranks: %f" % (num_ranks, t_end - t_start))

    if rank_id==0:
        model.eval()
        torch.save(model, '/tmp/mnist.pt')
        modelfile = '/tmp/mnist3.onnx'

        dummy_input = torch.randn(1, 1, 28, 28).to(device)
        input_names = [ "input_0" ]
        output_names = [ "output_0" ]
        dynamic_axes={'input_0' : {0 : 'batch_size'},'output_0' : {0 : 'batch_size'}}

        torch.onnx.export(model, dummy_input, modelfile, verbose=True, input_names=input_names, output_names=output_names, dynamic_axes=dynamic_axes)
        print("Exported to "+modelfile)

        import os
        import boto3
        from boto3 import session
    
        key_id = os.environ.get('AWS_ACCESS_KEY_ID')
        secret_key = os.environ.get('AWS_SECRET_ACCESS_KEY')
        endpoint_url = os.environ.get('AWS_S3_ENDPOINT')
        uploaded_file_name = os.environ.get('OUTPUT_PATH',os.uname()[1])
        session = boto3.session.Session(aws_access_key_id=key_id, aws_secret_access_key=secret_key)
        s3_client = boto3.client('s3', aws_access_key_id=key_id, aws_secret_access_key=secret_key,endpoint_url=endpoint_url,verify=False)
        buckets=s3_client.list_buckets()
        for bucket in buckets['Buckets']: print(bucket['Name'])
        s3_client.upload_file(modelfile, bucket['Name'],uploaded_file_name)
        print('uploaded_file_name',uploaded_file_name)
        print([item.get("Key") for item in s3_client.list_objects_v2(Bucket=bucket['Name']).get("Contents")])

# %% End of program
