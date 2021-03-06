import numpy as np
from PIL import Image
import os.path
import scipy.misc
import tensorflow as tf
import time
import json
from scipy.io import savemat
import wgancs_model

FLAGS = tf.app.flags.FLAGS
OUTPUT_TRAIN_SAMPLES = 0

def _summarize_progress(train_data, feature, label, gene_output, 
                        batch, suffix, max_samples=8, gene_param=None):
    
    td = train_data

    size = [label.shape[1], label.shape[2]]

    # complex input zpad into r and channel
    complex_zpad = tf.image.resize_nearest_neighbor(feature, size)
    complex_zpad = tf.maximum(tf.minimum(complex_zpad, 1.0), 0.0)

    # zpad magnitude
    if FLAGS.use_phase==True:
      mag_zpad = tf.sqrt(complex_zpad[:,:,:,0]**2+complex_zpad[:,:,:,1]**2)
    else:
      mag_zpad = tf.sqrt(complex_zpad[:,:,:,0]**2)
    mag_zpad = tf.maximum(tf.minimum(mag_zpad, 1.0), 0.0)
    mag_zpad = tf.reshape(mag_zpad, [FLAGS.batch_size,size[0],size[1],1])
    mag_zpad = tf.concat(axis=3, values=[mag_zpad, mag_zpad])
    
    # output image
    if FLAGS.use_phase==True:
      gene_output_complex = tf.complex(gene_output[:,:,:,0],gene_output[:,:,:,1])
    else:
      gene_output_complex = gene_output
    mag_output = tf.maximum(tf.minimum(tf.abs(gene_output_complex), 1.0), 0.0)
    mag_output = tf.reshape(mag_output, [FLAGS.batch_size, size[0], size[1], 1])
    #print('size_mag_output', mag)
    mag_output = tf.concat(axis=3, values=[mag_output, mag_output])

    if FLAGS.use_phase==True:
      label_complex = tf.complex(label[:,:,:,0], label[:,:,:,1])
    else:
      label_complex = label
    label_mag = tf.abs(label_complex)
    label_mag = tf.reshape(label_mag, [FLAGS.batch_size, size[0], size[1], 1])
    mag_gt = tf.concat(axis=3, values=[label_mag, label_mag]) #size (8, 160, 128, 2) 
    
    # calculate SSIM SNR and MSE for test images
    signal=mag_gt[:,20:size[0]-20,14:size[1]-14,:]    # crop out edges
    Gout=mag_output[:,20:size[0]-20,14:size[1]-14,:]
    SSIM=wgancs_model.loss_DSSIS_tf11(signal, Gout)
    signal=tf.reshape(signal[:,:,:,0],(FLAGS.batch_size,-1))   # and flatten
    Gout=tf.reshape(Gout[:,:,:,0],(FLAGS.batch_size,-1))    
    s_G=tf.abs(signal-Gout)
    SNR_output = 10*tf.reduce_sum(tf.log(tf.reduce_sum(signal**2,axis=1)/tf.reduce_sum(s_G**2,axis=1)))/tf.log(10.0)/FLAGS.batch_size
    MSE=tf.reduce_mean(s_G)   
    
    # concate for visualize image
    if FLAGS.use_phase==True:
      image = tf.concat(axis=2, values=[complex_zpad, mag_zpad, mag_output, mag_gt])
    else:
      image = tf.concat(axis=2, values=[mag_zpad, mag_output, mag_gt])
    image = image[0:max_samples,:,:,:]
    image = tf.concat(axis=0, values=[image[i,:,:,:] for i in range(int(max_samples))])
    snr_summary_op=tf.summary.merge_all()
    image,snr,mse,ssim = td.sess.run([image,SNR_output,MSE,SSIM])
    print('save to image size ', image.shape, 'type ', type(image))
    # 3rd channel for visualization
    mag_3rd = np.maximum(image[:,:,0],image[:,:,1])
    image = np.concatenate((image, mag_3rd[:,:,np.newaxis]),axis=2)

    # save to image file
    filename = 'batch%06d_%s.png' % (batch, suffix)
    filename = os.path.join(FLAGS.train_dir, filename)
    try:
      scipy.misc.toimage(image, cmin=0., cmax=1.).save(filename)
    except:
      import pilutil
      pilutil.toimage(image, cmin=0., cmax=1.).save(filename)
    print("    Saved %s" % (filename,))

    #gene_output_abs = np.abs(gene_output)
    # save layers and var_list
    if gene_param is not None:
        #add feature 
        print('dimension for input, ref, output:',
              feature.shape, label.shape, gene_output.shape)
        gene_param['feature'] = feature.tolist()
        gene_param['label'] = label.tolist()
        gene_param['gene_output'] = gene_output.tolist()
        # add input arguments
        # print(FLAGS.__dict__['__flags'])
        # gene_param['FLAGS'] = FLAGS.__dict__['__flags']

        # save json
        '''
        filename = 'batch%06d_%s.json' % (batch, suffix)
        filename = os.path.join(FLAGS.train_dir, filename)
        with open(filename, 'w') as outfile:
            json.dump(gene_param, outfile)
        print("    Saved %s" % (filename,))
        '''
    return snr,mse,ssim

def _save_checkpoint(train_data, batch):
    td = train_data

    oldname = 'checkpoint_old.txt'
    newname = 'checkpoint_new.txt'

    oldname = os.path.join(FLAGS.checkpoint_dir, oldname)
    newname = os.path.join(FLAGS.checkpoint_dir, newname)

    # Delete oldest checkpoint
    try:
        tf.gfile.Remove(oldname)
        tf.gfile.Remove(oldname + '.meta')
    except:
        pass

    # Rename old checkpoint
    try:
        tf.gfile.Rename(newname, oldname)
        tf.gfile.Rename(newname + '.meta', oldname + '.meta')
    except:
        pass

    # Generate new checkpoint
    saver = tf.train.Saver(sharded=True)
    filename=saver.save(td.sess, newname)

    print("Checkpoint saved:",filename)

def train_model(train_data, batchcount, num_sample_train=1984, num_sample_test=116):
    td = train_data
    summary_op = td.summary_op

    #td.sess.run(tf.global_variables_initializer())

    #TODO: load data

    lrval       = FLAGS.learning_rate_start
    start_time  = time.time()
    done  = False
    batch = batchcount
    # batch info    
    batch_size = FLAGS.batch_size
    num_batch_train = num_sample_train / batch_size
    num_batch_test = num_sample_test / batch_size            

    # learning rate
    assert FLAGS.learning_rate_half_life % 10 == 0

    # Cache test features and labels (they are small)    
    # update: get all test features
    list_test_features = []
    list_test_labels = []
    for batch_test in range(int(num_batch_test)):
        test_feature, test_label = td.sess.run([td.test_features, td.test_labels])
        list_test_features.append(test_feature)
        list_test_labels.append(test_label)
    print('prepare {0} test feature batches'.format(num_batch_test))
    # print([type(x) for x in list_test_features])
    # print([type(x) for x in list_test_labels])
    accumuated_err_loss=[]
    sum_writer=tf.summary.FileWriter(FLAGS.train_dir, td.sess.graph)
    while not done:
        batch += 1
        gene_ls_loss = gene_dc_loss = gene_loss = disc_real_loss = disc_fake_loss = -1.234

        #first train based on MSE and then GAN
        if batch < FLAGS.mse_batch:
           feed_dict = {td.learning_rate : lrval, td.gene_mse_factor : 1}
        elif batch <FLAGS.mse_batch+200:
           feed_dict = {td.learning_rate : lrval, td.gene_mse_factor : (FLAGS.mse_batch+200-batch)/float(200) } # for0.8: -0.001*batch + 4 }, for0.1: -0.0045*batch + 14.5 } 
        else:
	   #get rid of MSE loss
           feed_dict = {td.learning_rate : lrval, td.gene_mse_factor : 0 }

        
        # for training 
        # don't export var and layers for train to reduce size
        # move to later
        # ops = [td.gene_minimize, td.disc_minimize, td.gene_loss, td.disc_real_loss, td.disc_fake_loss, 
        #        td.train_features, td.train_labels, td.gene_output]#, td.gene_var_list, td.gene_layers]
        # _, _, gene_loss, disc_real_loss, disc_fake_loss, train_feature, train_label, train_output = td.sess.run(ops, feed_dict=feed_dict)

	# train disc multiple times
        for disc_iter in range(0):
            td.sess.run([td.disc_minimize],feed_dict=feed_dict)
	# then train both disc and gene once
        ops = [td.gene_minimize, td.disc_minimize, summary_op, td.gene_loss, td.gene_ls_loss, td.gene_dc_loss, td.disc_real_loss, td.disc_fake_loss, td.list_gene_losses]                   
        _, _, fet_sum,gene_loss, gene_ls_loss, gene_dc_loss, disc_real_loss, disc_fake_loss, list_gene_losses = td.sess.run(ops, feed_dict=feed_dict)
        sum_writer.add_summary(fet_sum,batch)
        
        # get all losses
        list_gene_losses = [float(x) for x in list_gene_losses]
        gene_mse_loss = list_gene_losses[1]   

        # verbose training progress
        if batch % 30 == 0:
            # Show we are alive
            elapsed = int(time.time() - start_time)/60
            err_log = 'Elapsed[{0:3f}], Batch [{1:1f}], G_Loss[{2}], G_mse_Loss[{3:3.3f}], G_LS_Loss[{4:3.3f}], G_DC_Loss[{5:3.3f}], D_Real_Loss[{6:3.3f}], D_Fake_Loss[{7:3.3f}]'.format(elapsed, batch, gene_loss, gene_mse_loss, gene_ls_loss, gene_dc_loss, disc_real_loss, disc_fake_loss)
            print(err_log)
            # update err loss
            err_loss = [int(batch), float(gene_loss), float(gene_dc_loss), 
                        float(gene_ls_loss), float(disc_real_loss), float(disc_fake_loss)]
            accumuated_err_loss.append(err_loss)
            # Finished?            
            current_progress = elapsed / FLAGS.train_time
            if (current_progress >= 1.0) or (batch > FLAGS.train_time*200):
                done = True
            
            # Update learning rate
            if batch % FLAGS.learning_rate_half_life == 0:
                lrval *= .5

        # export test batches
        if batch % FLAGS.summary_period == 0:
            # loop different test batch
            snr=mse=ssim=0
            for index_batch_test in range(int(num_batch_test)):
                # get test feature
                test_feature = list_test_features[index_batch_test]
                test_label = list_test_labels[index_batch_test]
            
                # Show progress with test features
                feed_dict = {td.gene_minput: test_feature}
                # not export var
                # ops = [td.gene_moutput, td.gene_mlayers, td.gene_var_list, td.disc_var_list, td.disc_layers]
                # gene_output, gene_layers, gene_var_list, disc_var_list, disc_layers= td.sess.run(ops, feed_dict=feed_dict)       
                
                ops = [td.gene_moutput, td.gene_mlayers]
                
                # get timing
                forward_passing_time = time.time()
                gene_output, gene_layers= td.sess.run(ops, feed_dict=feed_dict)       
                inference_time = time.time() - forward_passing_time
		
                # print('gene_var_list',[x.shape for x in gene_var_list])
                #print('gene_layers',[x.shape for x in gene_layers])
                #print("test time data consistency:", gene_dc_loss): add td.gene_dc_loss in ops
                # print('disc_var_list',[x.shape for x in disc_var_list])
                #print('disc_layers',[x.shape for x in disc_layers])

                # save record
                gene_param = {'train_log':err_log,
                              'train_loss':accumuated_err_loss,
                              'gene_loss':list_gene_losses,
                              'inference_time':inference_time,
                              'gene_layers':[x.tolist() for x in gene_layers if x.shape[-1]<10]}                
                # gene layers are too large
                if index_batch_test>0:
                    gene_param['gene_layers']=[]
                snr_b,mse_b,ssim_b=_summarize_progress(td, test_feature, test_label, gene_output, batch, 
                                    'test{0}'.format(index_batch_test),                                     
                                    max_samples = batch_size,
                                    gene_param = gene_param)
                snr+=snr_b
                mse+=mse_b
                ssim+=ssim_b
                # try to reduce mem
                gene_output = None
                gene_layers = None
                #disc_layers = None
                accumuated_err_loss = []
            print('SNR: ',snr/num_batch_test,'MSE: ',mse/num_batch_test,'SSIM: ',ssim/num_batch_test)
        # export train batches
        if OUTPUT_TRAIN_SAMPLES and (batch % FLAGS.summary_train_period == 0):
            # get train data
            ops = [td.gene_minimize, td.disc_minimize, td.gene_loss, td.gene_ls_loss, td.gene_dc_loss, td.disc_real_loss, td.disc_fake_loss, 
                   td.train_features, td.train_labels, td.gene_output]#, td.gene_var_list, td.gene_layers]
            _, _, gene_loss, gene_dc_loss, gene_ls_loss, disc_real_loss, disc_fake_loss, train_feature, train_label, train_output = td.sess.run(ops, feed_dict=feed_dict)
            print('train sample size:',train_feature.shape, train_label.shape, train_output.shape)
            _summarize_progress(td, train_feature, train_label, train_output, batch%num_batch_train, 'train')

        
        # export check points
        if batch % FLAGS.checkpoint_period == 0:
            # Save checkpoint
            _save_checkpoint(td, batch)

    _save_checkpoint(td, batch)
    print('Finished training!')
